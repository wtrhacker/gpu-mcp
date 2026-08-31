#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import gpu_mcp_reservations as reservations
from gpu_mcp_policy_approval import PolicyApprovalError, verify_policy_approved

MAX_POLICY_SEARCH_DEPTH = 32
POLICY_RECOVERY_TOOL_LEAVES = (
    "preview_policy_reload",
    "reload_policy",
    "reject_policy_reload",
)
ALLOWED_POLICY_RECOVERY_TOOL_NAMES = frozenset(
    tuple(f"mcp__gpu_cluster_mcp__{name}" for name in POLICY_RECOVERY_TOOL_LEAVES)
    + tuple(f"mcp__gpu-cluster-mcp__{name}" for name in POLICY_RECOVERY_TOOL_LEAVES)
    + tuple(f"gpu-cluster-mcp/{name}" for name in POLICY_RECOVERY_TOOL_LEAVES)
)
POLICY_EDIT_TOOL_NAMES = {"Edit", "MultiEdit"}
PATCH_TOOL_NAMES = {"functions.apply_patch", "apply_patch"}
HOOK_REMINDER_MODE_ENV = "GPU_MCP_HOOK_REMINDER_MODE"
HOOK_REMINDER_ADDITIONAL_CONTEXT = "additionalContext"
HOOK_REMINDER_OFF = "off"
TEST_POLICY_APPROVAL_STORE_ENV = "GPU_MCP_TEST_POLICY_APPROVAL_STORE"
TEST_HOOK_CAPABILITY_NONCE_ENV = "GPU_MCP_TEST_HOOK_CAPABILITY_NONCE"
TEST_STOP_STATE_ROOT_ENV = "GPU_MCP_TEST_STOP_STATE_ROOT"
STOP_LOCAL_SCAN_INTERVAL_SEC = 1.0
STOP_STATE_ROOT = Path.home() / "gpu-mcp" / "state" / "hook-stop"
TERMINAL_JOB_LIFECYCLES = {"succeeded", "failed", "finished"}


HOOK_MESSAGE = (
    "gpu-mcp.toml has changed but is not active.\n"
    "Do not revert it. Stop normal GPU work.\n\n"
    "If the human edited this file, call preview_policy_reload and show the raw "
    "preview output, including diff_summary and hashes.\n\n"
    "If you edited this file, tell the human you changed it, then call "
    "preview_policy_reload and show the raw preview output, including "
    "diff_summary and hashes. Ask the human to inspect gpu-mcp.toml before "
    "approving reload.\n\n"
    "Only edit gpu-mcp.toml while stale after explicit human rejection or "
    "cancellation of the prior candidate and explicit human re-orientation "
    "to the next candidate edit."
)

POLICY_BOOTSTRAP_MESSAGE = (
    "gpu-mcp.toml has not been activated yet. GPU MCP normal work is quarantined.\n\n"
    "Call preview_policy_reload and show the human the complete raw preview, "
    "including candidate_summary, diff_summary, and hashes. Call reload_policy "
    "only after explicit human approval.\n\n"
    "If the recovery tools are not visible yet, create or repair only this repo's "
    ".codex/config.toml registration, then ask the human to start or restart Codex "
    "from this trusted repo. This exception limits the path, not the TOML contents, "
    "so the human must inspect the complete config before restarting. Do not use "
    "gpu_mcp_doctor.py approve-policy as an agent-side shortcut."
)

POLICY_SYMLINK_MESSAGE = (
    "gpu-mcp.toml must not be a symlink.\n"
    "Do not edit the symlink target through the GPU MCP stale-policy recovery path."
)


def find_policy_file(cwd: str | Path, *, max_depth: int = MAX_POLICY_SEARCH_DEPTH) -> Path | None:
    path = Path(cwd).expanduser().resolve()
    if path.is_file():
        path = path.parent
    for depth, candidate_dir in enumerate((path, *path.parents)):
        if depth > max_depth:
            break
        policy = candidate_dir / "gpu-mcp.toml"
        if policy.exists() or policy.is_symlink():
            return policy
    return None


def _hook_block(message: str, *, policy_path: Path) -> dict:
    return {
        "decision": "block",
        "reason": f"{message}\n\nPolicy path: {policy_path}",
    }


def _is_policy_file_edit(
    tool_name: str,
    tool_input: object,
    *,
    policy_path: Path,
) -> bool:
    if tool_name not in POLICY_EDIT_TOOL_NAMES or not isinstance(tool_input, dict):
        return _is_policy_patch_edit(tool_name, tool_input, policy_path=policy_path)
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    return _path_matches_policy(raw_path, policy_path=policy_path)


def _path_matches_policy(
    raw_path: object,
    *,
    policy_path: Path,
    relative_to: Path | None = None,
) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    if policy_path.is_symlink():
        return False
    try:
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = ((relative_to or policy_path.parent) / target).resolve()
        else:
            target = target.resolve()
    except OSError:
        return False
    return target == policy_path.resolve()


def _is_policy_patch_edit(
    tool_name: str,
    tool_input: object,
    *,
    policy_path: Path,
) -> bool:
    if tool_name not in PATCH_TOOL_NAMES:
        return False
    if isinstance(tool_input, dict):
        patch = tool_input.get("patch") or tool_input.get("cmd") or tool_input.get("command")
    else:
        patch = tool_input
    if not isinstance(patch, str) or not patch:
        return False
    touched: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("*** Add File: ", "*** Delete File: ", "*** Move to: ")):
            return False
        if line.startswith("*** Update File: "):
            touched.append(line.removeprefix("*** Update File: ").strip())
    return bool(touched) and all(
        _path_matches_policy(path, policy_path=policy_path) for path in touched
    )


def _is_bootstrap_codex_config_edit(
    tool_name: str,
    tool_input: object,
    *,
    policy_path: Path,
) -> bool:
    """Allow only the repo MCP registration file to be repaired before activation."""
    codex_config = policy_path.parent / ".codex" / "config.toml"
    if (policy_path.parent / ".codex").is_symlink() or codex_config.is_symlink():
        return False
    if tool_name in POLICY_EDIT_TOOL_NAMES and isinstance(tool_input, dict):
        raw_path = tool_input.get("file_path") or tool_input.get("path")
        return _path_matches_policy(
            raw_path,
            policy_path=codex_config,
            relative_to=policy_path.parent,
        )
    if tool_name not in PATCH_TOOL_NAMES:
        return False
    if isinstance(tool_input, dict):
        patch = tool_input.get("patch") or tool_input.get("cmd") or tool_input.get("command")
    else:
        patch = tool_input
    if not isinstance(patch, str) or not patch:
        return False
    touched: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("*** Delete File: ", "*** Move to: ")):
            return False
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            touched.append(line.split(": ", 1)[1].strip())
    return bool(touched) and all(
        _path_matches_policy(
            path,
            policy_path=codex_config,
            relative_to=policy_path.parent,
        )
        for path in touched
    )


def check_policy_drift(
    cwd: str | Path,
    *,
    store_path: str | Path | None = None,
    tool_name: str = "",
    tool_input: object = None,
) -> dict | None:
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return None
    if policy_path.is_symlink():
        return _hook_block(POLICY_SYMLINK_MESSAGE, policy_path=policy_path)
    if tool_name in ALLOWED_POLICY_RECOVERY_TOOL_NAMES:
        return None
    try:
        verify_policy_approved(policy_path, store_path=store_path)
    except PolicyApprovalError as exc:
        if _is_policy_file_edit(tool_name, tool_input, policy_path=policy_path):
            return None
        initial_bootstrap = str(exc).startswith("policy is not approved:")
        if initial_bootstrap and _is_bootstrap_codex_config_edit(
            tool_name,
            tool_input,
            policy_path=policy_path,
        ):
            return None
        message = POLICY_BOOTSTRAP_MESSAGE if initial_bootstrap else HOOK_MESSAGE
        return _hook_block(message, policy_path=policy_path)
    return None


def _parse_hook_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hook_now(*, env: dict[str, str] | None = None) -> datetime:
    source = os.environ if env is None else env
    injected = source.get("GPU_MCP_TEST_NOW", "").strip()
    if injected and source.get("PYTEST_CURRENT_TEST"):
        parsed = _parse_hook_time(injected)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _read_json_quiet(path: Path) -> object | None:
    try:
        return reservations.read_json_file_no_follow(path)
    except Exception:
        return None


def _retire_hook_reminder(repo_root: Path, job_id: object) -> None:
    if not reservations.is_job_id(job_id):
        return
    try:
        reservations.hook_reminder_path(repo_root, str(job_id)).unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def _iso_hook_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _reservation_metadata_for_record(record: dict) -> dict | None:
    reservation_key = record.get("reservation_key")
    if not reservations.is_reservation_key(reservation_key):
        return None
    metadata_path = (
        reservations.reservation_dir(
            reservations.reservation_registry_root(),
            str(reservation_key),
        )
        / "metadata.json"
    )
    metadata = _read_json_quiet(metadata_path)
    if not isinstance(metadata, dict):
        return None
    if (
        metadata.get("job_id") != record.get("job_id")
        or metadata.get("attempt_id") != record.get("active_attempt_id")
        or metadata.get("reservation_key") != reservation_key
        or metadata.get("server_instance_id") != record.get("server_instance_id")
    ):
        return None
    return metadata


def _poll_interval_sec(record: dict) -> int:
    raw = record.get("poll_interval_sec")
    if isinstance(raw, bool):
        interval = reservations.DEFAULT_POLL_INTERVAL_SEC
    else:
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = reservations.DEFAULT_POLL_INTERVAL_SEC
    return (
        interval
        if interval >= reservations.MIN_POLL_INTERVAL_SEC
        else reservations.DEFAULT_POLL_INTERVAL_SEC
    )


def _status_acknowledged(record: dict, due_at: datetime) -> bool:
    if record.get("job_lifecycle") in {"succeeded", "failed", "finished"}:
        return True
    checked_at = _parse_hook_time(record.get("last_status_checked_at"))
    return checked_at is not None and checked_at >= due_at


def _claim_job_reminder(
    repo_root: Path,
    record: dict,
    *,
    kind: str,
    due_at: datetime | None,
    now: datetime,
) -> bool | None:
    job_id = record.get("job_id")
    if not reservations.is_job_id(job_id):
        return False
    try:
        reminder_path = reservations.hook_reminder_path(repo_root, str(job_id))
        lock_path = reminder_path.with_suffix(".lock")
        reservations._assert_no_symlink_ancestors(lock_path.parent)
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        reservations._assert_no_symlink_ancestors(lock_path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
    except Exception:
        return None
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            reminder_path = reservations.hook_reminder_path(repo_root, str(job_id))
            reminder = _read_json_quiet(reminder_path)
            if not isinstance(reminder, dict):
                reminder = {}
            if kind == "poll_due" and (
                due_at is None or _status_acknowledged(record, due_at)
            ):
                return False
            same_attempt = (
                reminder.get("attempt_id") == record.get("active_attempt_id")
                and reminder.get("reservation_key") == record.get("reservation_key")
            )
            if kind == "outcome":
                same_event = (
                    same_attempt
                    and reminder.get("last_outcome_attempt_id")
                    == record.get("active_attempt_id")
                )
                last_reminded_key = "last_outcome_reminded_at"
                count_key = "outcome_reminder_count"
            else:
                same_event = (
                    same_attempt
                    and reminder.get("last_reminded_poll_after")
                    == record.get("next_poll_after")
                )
                last_reminded_key = "last_reminded_at"
                count_key = "reminder_count_for_poll_after"

            count = 0
            if same_event:
                last_reminded_at = _parse_hook_time(reminder.get(last_reminded_key))
                if last_reminded_at is not None:
                    elapsed = (now - last_reminded_at).total_seconds()
                    if elapsed < _poll_interval_sec(record):
                        return False
                raw_count = reminder.get(count_key)
                if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
                    count = raw_count
            updated = {
                **reminder,
                "schema_version": 1,
                "job_id": job_id,
                "attempt_id": record.get("active_attempt_id"),
                "reservation_key": record.get("reservation_key"),
            }
            if kind == "outcome":
                updated.update(
                    {
                        "last_outcome_attempt_id": record.get("active_attempt_id"),
                        "last_outcome_reminded_at": _iso_hook_time(now),
                        "outcome_reminder_count": count + 1,
                    }
                )
            else:
                updated.update(
                    {
                        "last_reminded_poll_after": record.get("next_poll_after"),
                        "last_reminded_at": _iso_hook_time(now),
                        "reminder_count_for_poll_after": count + 1,
                    }
                )
            try:
                reservations.atomic_write_json(reminder_path, updated)
            except Exception:
                return None
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reminder_output(
    records: list[tuple[dict, datetime | None, str]],
    *,
    now: datetime,
) -> dict:
    records = sorted(
        records,
        key=lambda item: (
            item[2] != "outcome",
            item[1].timestamp() if isinstance(item[1], datetime) else float("inf"),
            str(item[0].get("job_id")),
        ),
    )
    count = len(records)
    kinds = {kind for _record, _due_at, kind in records}
    if kinds == {"poll_due"}:
        heading = (
            "GPU MCP: 1 managed job has a scheduled status check due."
            if count == 1
            else f"GPU MCP: {count} managed jobs have scheduled status checks due."
        )
    elif kinds == {"outcome"}:
        heading = (
            "GPU MCP: 1 managed job has a local outcome to reconcile through status."
            if count == 1
            else f"GPU MCP: {count} managed jobs have local outcomes to reconcile through status."
        )
    else:
        heading = f"GPU MCP: {count} managed jobs have status events to reconcile."
    lines = [heading]
    for record, due_at, kind in records:
        job_id = record.get("job_id")
        if kind == "outcome":
            lines.append(
                f'- {job_id}: local outcome detected; '
                f'call manage_gpu_job(action="status", job_id="{job_id}").'
            )
            continue
        assert due_at is not None
        overdue_min = max(0, int((now - due_at).total_seconds() // 60))
        lines.append(
            f'- {job_id}: scheduled status check is overdue by {overdue_min}m; '
            f'call manage_gpu_job(action="status", job_id="{job_id}").'
        )
    lines.append("Continue from the returned lifecycle.")
    context = "\n".join(lines)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        },
    }


def _parse_next_poll_after(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z") or " " in value:
        return None
    return _parse_hook_time(value)


def _nonempty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _targeted_status_call(
    tool_name: str,
    tool_input: object,
) -> tuple[str | None, str | None]:
    if not str(tool_name or "").endswith("manage_gpu_job") or not isinstance(tool_input, dict):
        return None, None
    action = tool_input.get("action", "status")
    if not isinstance(action, str) or action.strip().lower() != "status":
        return None, None
    job_id = tool_input.get("job_id")
    reservation_key = tool_input.get("reservation_key")
    if job_id is not None and not reservations.is_job_id(job_id):
        return None, None
    if reservation_key is not None and not reservations.is_reservation_key(reservation_key):
        return None, None
    if reservations.is_job_id(job_id):
        return str(job_id), None
    if reservations.is_reservation_key(reservation_key):
        return None, str(reservation_key)
    return None, None


def check_test_hook_capability_nonce(*, env: dict[str, str] | None = None) -> dict | None:
    source = os.environ if env is None else env
    nonce = source.get(TEST_HOOK_CAPABILITY_NONCE_ENV, "").strip()
    if not nonce or not source.get("PYTEST_CURRENT_TEST"):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": nonce,
        },
    }


def check_gpu_job_reminders(
    cwd: str | Path,
    *,
    tool_name: str = "",
    tool_input: object = None,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> dict | None:
    source = os.environ if env is None else env
    if source.get(HOOK_REMINDER_MODE_ENV, HOOK_REMINDER_ADDITIONAL_CONTEXT) == HOOK_REMINDER_OFF:
        return None
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return None
    repo_root = policy_path.parent.resolve()
    jobs_root = reservations.repo_job_state_root(repo_root)
    if not jobs_root.exists() or jobs_root.is_symlink():
        return None
    current = now or _hook_now(env=env)
    targeted_job_id, targeted_reservation_key = _targeted_status_call(
        tool_name,
        tool_input,
    )
    reminder_records: list[tuple[dict, datetime | None, str]] = []

    for job_dir in sorted(jobs_root.glob("job-*")):
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        job_file = job_dir / "job.json"
        record = _read_json_quiet(job_file)
        if not isinstance(record, dict):
            continue
        job_id = record.get("job_id")
        attempt_id = record.get("active_attempt_id")
        next_poll_after = record.get("next_poll_after")
        due_at = _parse_hook_time(next_poll_after)
        if (
            not reservations.is_job_id(job_id)
            or job_dir.name != job_id
            or not reservations.is_attempt_id(attempt_id)
            or record.get("active_reservation", True) is False
            or record.get("job_lifecycle") in TERMINAL_JOB_LIFECYCLES
        ):
            continue
        if (
            (targeted_job_id is not None and job_id == targeted_job_id)
            or (
                targeted_reservation_key is not None
                and record.get("reservation_key") == targeted_reservation_key
            )
        ):
            continue
        metadata = _reservation_metadata_for_record(record)
        if metadata is None or metadata.get("repo") != str(repo_root):
            _retire_hook_reminder(repo_root, job_id)
            continue

        try:
            reservations.outcome_record_path(repo_root, job_id, attempt_id).lstat()
            outcome_present = True
        except OSError:
            outcome_present = False
        poll_due = (
            due_at is not None
            and due_at <= current
            and not _status_acknowledged(record, due_at)
        )
        if not outcome_present and not poll_due:
            continue

        kind = "outcome" if outcome_present else "poll_due"
        claimed = _claim_job_reminder(
            repo_root,
            record,
            kind=kind,
            due_at=due_at,
            now=current,
        )
        if not claimed:
            continue
        reminder_records.append((record, due_at, kind))
    if not reminder_records:
        return None
    return _reminder_output(reminder_records, now=current)


def _stop_state_root(*, env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    injected = source.get(TEST_STOP_STATE_ROOT_ENV, "").strip()
    if injected and source.get("PYTEST_CURRENT_TEST"):
        return Path(injected).expanduser().absolute()
    return STOP_STATE_ROOT


def _stop_state_path(
    repo_root: Path,
    session_id: str,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    key = hashlib.sha256(f"{repo_root}\0{session_id}".encode()).hexdigest()
    return _stop_state_root(env=env) / f"{key}.json"


def _claim_stop_events(
    repo_root: Path,
    *,
    session_id: str,
    turn_id: str,
    events: list[dict],
    env: dict[str, str] | None = None,
) -> list[dict] | None:
    """Claim each advisory event once within one interactive turn."""
    try:
        state_path = _stop_state_path(repo_root, session_id, env=env)
        state_root = state_path.parent
        reservations._assert_no_symlink_ancestors(state_root)
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        reservations._assert_no_symlink_ancestors(state_root)
        lock_path = state_path.with_suffix(".lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_path, flags, 0o600)
    except Exception:
        return None

    try:
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_json_quiet(state_path)
                turn_key = hashlib.sha256(turn_id.encode()).hexdigest()
                if not isinstance(state, dict) or state.get("turn_key") != turn_key:
                    delivered: dict[str, str] = {}
                else:
                    raw_delivered = state.get("delivered")
                    delivered = dict(raw_delivered) if isinstance(raw_delivered, dict) else {}

                claimed: list[dict] = []
                for event in events:
                    job_id = event.get("job_id")
                    event_key = event.get("event_key")
                    if not reservations.is_job_id(job_id) or not isinstance(event_key, str):
                        continue
                    if delivered.get(str(job_id)) == event_key:
                        continue
                    delivered[str(job_id)] = event_key
                    claimed.append(event)
                if not claimed:
                    return []

                reservations.atomic_write_json(
                    state_path,
                    {
                        "schema_version": 1,
                        "repo": str(repo_root),
                        "turn_key": turn_key,
                        "delivered": delivered,
                    },
                )
                return claimed
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        return None


def _stop_job_candidates(repo_root: Path) -> list[dict]:
    jobs_root = reservations.repo_job_state_root(repo_root)
    if not jobs_root.exists() or jobs_root.is_symlink():
        return []

    candidates: list[dict] = []
    for job_dir in sorted(jobs_root.glob("job-*")):
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        record = _read_json_quiet(job_dir / "job.json")
        if not isinstance(record, dict):
            continue
        job_id = record.get("job_id")
        attempt_id = record.get("active_attempt_id")
        if (
            not reservations.is_job_id(job_id)
            or job_dir.name != job_id
            or not reservations.is_attempt_id(attempt_id)
            or record.get("active_reservation", True) is False
            or record.get("job_lifecycle") in TERMINAL_JOB_LIFECYCLES
        ):
            continue
        metadata = _reservation_metadata_for_record(record)
        if metadata is None or metadata.get("repo") != str(repo_root):
            continue

        try:
            outcome_path = reservations.outcome_record_path(repo_root, job_id, attempt_id)
            outcome_path.lstat()
            outcome_present = True
        except OSError:
            outcome_present = False
        due_at = _parse_next_poll_after(record.get("next_poll_after"))
        if due_at is None and not outcome_present:
            continue
        candidates.append(
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "next_poll_after": record.get("next_poll_after"),
                "due_at": due_at,
                "outcome_present": outcome_present,
            }
        )
    return candidates


def probe_stop_wait_ownership(cwd: str | Path) -> dict:
    """Report whether a managed GPU wait can own the next Stop event.

    This is an immediate, read-only probe for another synchronous Stop hook. It
    deliberately does not claim an event or wait for one; the normal GPU Stop
    handler remains the lifecycle authority.
    """
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return {
            "schema_version": 1,
            "owner": "gpu_mcp_wait",
            "owns_stop": False,
            "job_ids": [],
        }
    repo_root = policy_path.parent.resolve()
    candidates = _stop_job_candidates(repo_root)
    return {
        "schema_version": 1,
        "owner": "gpu_mcp_wait",
        "owns_stop": bool(candidates),
        "job_ids": sorted(str(candidate["job_id"]) for candidate in candidates),
    }


def _ready_stop_events(candidates: list[dict], *, now: datetime) -> list[dict]:
    events: list[dict] = []
    for candidate in candidates:
        job_id = str(candidate["job_id"])
        attempt_id = str(candidate["attempt_id"])
        if candidate["outcome_present"]:
            events.append({
                "job_id": job_id,
                "kind": "outcome",
                "event_key": f"outcome:{attempt_id}",
            })
            continue
        due_at = candidate.get("due_at")
        if isinstance(due_at, datetime) and due_at <= now:
            poll_after = candidate.get("next_poll_after")
            events.append({
                "job_id": job_id,
                "kind": "poll_due",
                "event_key": f"poll:{attempt_id}:{poll_after}",
            })
    return sorted(events, key=lambda event: (event["kind"] != "outcome", event["job_id"]))


def _stop_continuation(events: list[dict]) -> dict:
    lines = ["GPU MCP: managed-job status is due."]
    for event in events:
        job_id = event["job_id"]
        reason = (
            "local outcome detected"
            if event["kind"] == "outcome"
            else "scheduled status check is due"
        )
        lines.append(
            f'- {job_id}: {reason}; call manage_gpu_job(action="status", '
            f'job_id="{job_id}").'
        )
    lines.append("Status reconciles managed state; continue from the returned lifecycle.")
    return {"decision": "block", "reason": "\n".join(lines)}


def wait_for_gpu_job_stop_event(
    cwd: str | Path,
    *,
    session_id: str,
    turn_id: str,
    env: dict[str, str] | None = None,
    now_fn=None,
    sleep_fn=time.sleep,
    scan_interval_sec: float = STOP_LOCAL_SCAN_INTERVAL_SEC,
    max_wait_sec: float | None = None,
) -> dict | None:
    """Suspend a Stop hook until a local outcome or scheduled status event."""
    if not _nonempty_text(session_id) or not _nonempty_text(turn_id):
        return None
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return None
    repo_root = policy_path.parent.resolve()
    source = os.environ if env is None else env
    clock = now_fn or (lambda: _hook_now(env=source))
    started = time.monotonic()
    scan_interval = max(0.01, float(scan_interval_sec))

    try:
        while True:
            current = clock()
            candidates = _stop_job_candidates(repo_root)
            if not candidates:
                return None

            ready = _ready_stop_events(candidates, now=current)
            if ready:
                ready_job_ids = {event["job_id"] for event in ready}
                claimed = _claim_stop_events(
                    repo_root,
                    session_id=session_id,
                    turn_id=turn_id,
                    events=ready,
                    env=source,
                )
                if claimed is None:
                    return None
                if claimed:
                    return _stop_continuation(claimed)
                if all(candidate["job_id"] in ready_job_ids for candidate in candidates):
                    return None

            if max_wait_sec is not None:
                remaining = float(max_wait_sec) - (time.monotonic() - started)
                if remaining <= 0:
                    return None
            else:
                remaining = None

            future_delays = [
                (candidate["due_at"] - current).total_seconds()
                for candidate in candidates
                if isinstance(candidate.get("due_at"), datetime) and candidate["due_at"] > current
            ]
            sleep_for = min(scan_interval, min(future_delays, default=scan_interval))
            if remaining is not None:
                sleep_for = min(sleep_for, remaining)
            if sleep_for <= 0:
                return None
            sleep_fn(sleep_for)
    except Exception:
        return None


def _store_from_test_env(*, env: dict[str, str] | None = None) -> Path | None:
    source = os.environ if env is None else env
    raw = source.get(TEST_POLICY_APPROVAL_STORE_ENV, "").strip()
    if raw and source.get("PYTEST_CURRENT_TEST"):
        return Path(raw).expanduser()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--probe-stop-wait-ownership", action="store_true")
    parser.add_argument("--cwd", type=Path, default=None)
    args = parser.parse_args(argv)
    store_path = args.store or _store_from_test_env()

    if args.probe_stop_wait_ownership:
        try:
            result = probe_stop_wait_ownership(args.cwd or Path.cwd())
        except Exception:
            return 1
        print(json.dumps(result, sort_keys=True))
        return 0

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        event = {}
    cwd = event.get("cwd") or "."
    result = check_policy_drift(
        cwd,
        store_path=store_path,
        tool_name=str(event.get("tool_name") or ""),
        tool_input=(
            event.get("tool_input")
            or event.get("toolInput")
            or event.get("arguments")
            or {"cmd": event.get("command")}
        ),
    )
    event_name = event.get("hook_event_name") or event.get("hookEventName")
    if result is None and event_name == "PreToolUse":
        tool_name = str(event.get("tool_name") or "")
        tool_input = (
            event.get("tool_input")
            or event.get("toolInput")
            or event.get("arguments")
            or {"cmd": event.get("command")}
        )
        result = (
            check_test_hook_capability_nonce()
            or check_gpu_job_reminders(
                cwd,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        )
    if result is None and event_name == "Stop":
        result = wait_for_gpu_job_stop_event(
            cwd,
            session_id=str(event.get("session_id") or ""),
            turn_id=str(event.get("turn_id") or ""),
        )
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
