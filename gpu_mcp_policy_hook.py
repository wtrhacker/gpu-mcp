#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import gpu_mcp_reservations as reservations
from gpu_mcp_policy_approval import PolicyApprovalError, verify_policy_approved

MAX_POLICY_SEARCH_DEPTH = 32
ALLOWED_STALE_TOOL_SUFFIXES = (
    "preview_policy_reload",
    "reload_policy",
    "reject_policy_reload",
)
POLICY_EDIT_TOOL_NAMES = {"Edit", "MultiEdit"}
PATCH_TOOL_NAMES = {"functions.apply_patch", "apply_patch"}
HOOK_REMINDER_MODE_ENV = "GPU_MCP_HOOK_REMINDER_MODE"
HOOK_REMINDER_ADDITIONAL_CONTEXT = "additionalContext"
HOOK_REMINDER_OFF = "off"
TEST_POLICY_APPROVAL_STORE_ENV = "GPU_MCP_TEST_POLICY_APPROVAL_STORE"
TEST_HOOK_CAPABILITY_NONCE_ENV = "GPU_MCP_TEST_HOOK_CAPABILITY_NONCE"


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


def _path_matches_policy(raw_path: object, *, policy_path: Path) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    if policy_path.is_symlink():
        return False
    try:
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = (policy_path.parent / target).resolve()
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
    if any(tool_name.endswith(suffix) for suffix in ALLOWED_STALE_TOOL_SUFFIXES):
        return None
    try:
        verify_policy_approved(policy_path, store_path=store_path)
    except PolicyApprovalError:
        if _is_policy_file_edit(tool_name, tool_input, policy_path=policy_path):
            return None
        return _hook_block(HOOK_MESSAGE, policy_path=policy_path)
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


def _heartbeat_interval_sec(metadata: dict) -> int:
    raw = metadata.get("heartbeat_interval_sec")
    if isinstance(raw, bool):
        interval = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC
    else:
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC
    return max(
        reservations.MIN_HEARTBEAT_INTERVAL_SEC,
        min(reservations.MAX_HEARTBEAT_INTERVAL_SEC, interval),
    )


def _status_acknowledged(record: dict, due_at: datetime) -> bool:
    if record.get("job_lifecycle") in {"succeeded", "failed", "finished"}:
        return True
    checked_at = _parse_hook_time(record.get("last_status_checked_at"))
    return checked_at is not None and checked_at >= due_at


def _claim_due_reminder(
    repo_root: Path,
    record: dict,
    metadata: dict,
    *,
    due_at: datetime,
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
            if _status_acknowledged(record, due_at):
                return False
            same_poll = (
                reminder.get("last_reminded_poll_after") == record.get("next_poll_after")
                and reminder.get("attempt_id") == record.get("active_attempt_id")
                and reminder.get("reservation_key") == record.get("reservation_key")
            )
            count = 0
            if same_poll:
                last_reminded_at = _parse_hook_time(reminder.get("last_reminded_at"))
                if last_reminded_at is not None:
                    elapsed = (now - last_reminded_at).total_seconds()
                    if elapsed < _heartbeat_interval_sec(metadata):
                        return False
                raw_count = reminder.get("reminder_count_for_poll_after")
                if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
                    count = raw_count
            try:
                reservations.atomic_write_json(
                    reminder_path,
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "attempt_id": record.get("active_attempt_id"),
                        "reservation_key": record.get("reservation_key"),
                        "last_reminded_poll_after": record.get("next_poll_after"),
                        "last_reminded_at": _iso_hook_time(now),
                        "reminder_count_for_poll_after": count + 1,
                    },
                )
            except Exception:
                return None
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reminder_output(records: list[tuple[dict, datetime]], *, now: datetime) -> dict:
    records = sorted(records, key=lambda item: (item[1], str(item[0].get("job_id"))))
    count = len(records)
    lines = [
        (
            "GPU MCP: 1 managed job is due for status."
            if count == 1
            else f"GPU MCP: {count} managed jobs are due for status."
        )
    ]
    for record, due_at in records:
        job_id = record.get("job_id")
        overdue_min = max(0, int((now - due_at).total_seconds() // 60))
        lines.append(
            f'- {job_id}: overdue by {overdue_min}m; '
            f'call manage_gpu_job(action="status", job_id="{job_id}") '
            "before using this job's outputs."
        )
    lines.append("Independent work may continue; output-dependent work must wait for terminal status.")
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


def _phase7_output(context: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        },
    }


def _tool_suffix(tool_name: str) -> str:
    return tool_name.rsplit("/", 1)[-1]


def _nonempty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _phase7_role(tool_input: dict) -> str:
    raw_role = tool_input.get("job_role")
    if isinstance(raw_role, str) and raw_role.strip().lower() in {"smoke", "main", "one_off"}:
        return raw_role.strip().lower()
    return "main" if tool_input.get("async_mode") is True else "one_off"


def _phase7_launch_guidance(tool_input: object) -> dict | None:
    if not isinstance(tool_input, dict):
        return None
    if _phase7_role(tool_input) != "main":
        return None
    if reservations.is_job_id(tool_input.get("smoke_job_id")):
        return None
    if _nonempty_text(tool_input.get("smoke_skip_reason")) is not None:
        return None
    context = (
        "GPU MCP: you are about to launch a main/background GPU job without smoke evidence. "
        "Before proceeding, either run run_python_on_gpu(job_role=\"smoke\", ...) with small "
        "representative inputs, or retry the main launch with smoke_skip_reason explaining why "
        "a smoke check is not useful. Cadence hints such as expected_duration_sec or "
        "cadence_hint_sec help schedule checks, but do not replace smoke evidence or a skip reason."
    )
    return _phase7_output(context)


def _phase7_status_guidance(
    repo_root: Path,
    tool_input: object,
    *,
    now: datetime,
) -> dict | None:
    if not isinstance(tool_input, dict):
        return None
    if str(tool_input.get("action") or "").strip().lower() != "status":
        return None
    if _nonempty_text(tool_input.get("early_poll_reason")) is not None:
        return None
    job_id = tool_input.get("job_id")
    if not reservations.is_job_id(job_id):
        return None
    record = _read_json_quiet(reservations.job_record_path(repo_root, str(job_id)))
    if not isinstance(record, dict):
        return None
    if record.get("job_lifecycle") in {"succeeded", "failed", "finished"}:
        return None
    next_poll_after = record.get("next_poll_after")
    due_at = _parse_next_poll_after(next_poll_after)
    if due_at is None or due_at <= now:
        return None
    context = (
        f"GPU MCP: job {job_id} is not due for status until {next_poll_after}. "
        "Do independent work or wait; do not use this job's outputs yet. "
        f"If a full check is justified now, call manage_gpu_job(action=\"status\", "
        f"job_id=\"{job_id}\", early_poll_reason=\"...\")."
    )
    return _phase7_output(context)


def check_phase7_pretooluse_guidance(
    cwd: str | Path,
    *,
    tool_name: str,
    tool_input: object,
    now: datetime | None = None,
) -> dict | None:
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return None
    repo_root = policy_path.parent.resolve()
    suffix = _tool_suffix(str(tool_name or ""))
    if suffix == "run_python_on_gpu":
        return _phase7_launch_guidance(tool_input)
    if suffix == "manage_gpu_job":
        return _phase7_status_guidance(repo_root, tool_input, now=now or _hook_now())
    return None


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
    due_records: list[tuple[dict, datetime]] = []

    for job_dir in sorted(jobs_root.glob("job-*")):
        if job_dir.is_symlink() or not job_dir.is_dir():
            continue
        job_file = job_dir / "job.json"
        record = _read_json_quiet(job_file)
        if not isinstance(record, dict):
            continue
        job_id = record.get("job_id")
        next_poll_after = record.get("next_poll_after")
        due_at = _parse_hook_time(next_poll_after)
        if not reservations.is_job_id(job_id) or due_at is None or due_at > current:
            continue
        if _status_acknowledged(record, due_at):
            continue
        metadata = _reservation_metadata_for_record(record)
        if metadata is None:
            _retire_hook_reminder(repo_root, job_id)
            continue
        claimed = _claim_due_reminder(repo_root, record, metadata, due_at=due_at, now=current)
        if not claimed:
            continue
        due_records.append((record, due_at))
    if not due_records:
        return None
    return _reminder_output(due_records, now=current)


def _store_from_test_env(*, env: dict[str, str] | None = None) -> Path | None:
    source = os.environ if env is None else env
    raw = source.get(TEST_POLICY_APPROVAL_STORE_ENV, "").strip()
    if raw and source.get("PYTEST_CURRENT_TEST"):
        return Path(raw).expanduser()
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, default=None)
    args = parser.parse_args(argv)
    store_path = args.store or _store_from_test_env()

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
    if result is None and event.get("hook_event_name") == "PreToolUse":
        tool_name = str(event.get("tool_name") or "")
        tool_input = (
            event.get("tool_input")
            or event.get("toolInput")
            or event.get("arguments")
            or {"cmd": event.get("command")}
        )
        result = (
            check_test_hook_capability_nonce()
            or check_phase7_pretooluse_guidance(
                cwd,
                tool_name=tool_name,
                tool_input=tool_input,
            )
            or check_gpu_job_reminders(cwd)
        )
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
