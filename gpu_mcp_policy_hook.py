#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gpu_mcp_policy_approval import PolicyApprovalError, verify_policy_approved

MAX_POLICY_SEARCH_DEPTH = 32
ALLOWED_STALE_TOOL_SUFFIXES = (
    "preview_policy_reload",
    "reload_policy",
    "reject_policy_reload",
)


HOOK_MESSAGE = (
    "gpu-mcp.toml has changed but is not active.\n"
    "Do not revert it. Stop normal GPU work.\n\n"
    "If the human edited this file, call preview_policy_reload and show the raw "
    "preview output, including diff_summary and hashes.\n\n"
    "If you edited this file, tell the human you changed it, then call "
    "preview_policy_reload and show the raw preview output, including "
    "diff_summary and hashes. Ask the human to inspect gpu-mcp.toml before "
    "approving reload."
)


def find_policy_file(cwd: str | Path, *, max_depth: int = MAX_POLICY_SEARCH_DEPTH) -> Path | None:
    path = Path(cwd).expanduser().resolve()
    if path.is_file():
        path = path.parent
    for depth, candidate_dir in enumerate((path, *path.parents)):
        if depth > max_depth:
            break
        policy = candidate_dir / "gpu-mcp.toml"
        if policy.exists():
            return policy
    return None


def _hook_block(message: str, *, policy_path: Path) -> dict:
    return {
        "decision": "block",
        "reason": f"{message}\n\nPolicy path: {policy_path}",
    }


def check_policy_drift(
    cwd: str | Path,
    *,
    store_path: str | Path | None = None,
    tool_name: str = "",
) -> dict | None:
    if any(tool_name.endswith(suffix) for suffix in ALLOWED_STALE_TOOL_SUFFIXES):
        return None
    policy_path = find_policy_file(cwd)
    if policy_path is None:
        return None
    try:
        verify_policy_approved(policy_path, store_path=store_path)
    except PolicyApprovalError:
        return _hook_block(HOOK_MESSAGE, policy_path=policy_path)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        event = {}
    cwd = event.get("cwd") or "."
    result = check_policy_drift(
        cwd,
        store_path=args.store,
        tool_name=str(event.get("tool_name") or ""),
    )
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
