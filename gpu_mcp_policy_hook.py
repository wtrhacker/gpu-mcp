#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
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


def probe_stop_wait_ownership(cwd: str | Path) -> dict:
    """Keep the research-hook handoff contract; GPU hooks never own Stop now.

    Monitoring belongs exclusively to the shared backend. Do not inspect repo
    jobs or Codex configuration to select another monitoring implementation.
    """
    return {"schema_version": 1, "owner": "gpu_mcp_wait", "owns_stop": False, "job_ids": []}


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
        result = check_test_hook_capability_nonce()
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
