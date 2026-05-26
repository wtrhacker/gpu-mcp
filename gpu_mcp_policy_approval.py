from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpu_mcp_config import GpuMcpPolicy, load_policy


class PolicyApprovalError(ValueError):
    """Raised when a repo policy is not in the approved-policy record."""


def default_store_path() -> Path:
    return Path.home() / "gpu-mcp" / "state" / "approved-policies.json"


def policy_file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).resolve().read_bytes()).hexdigest()


def policy_summary(policy: GpuMcpPolicy) -> dict[str, Any]:
    return {
        "repo_root": str(policy.repo_root),
        "nodes": list(policy.nodes),
        "script_roots": [str(path) for path in policy.script_roots],
        "write_roots": [str(path) for path in policy.write_roots],
        "output_roots": [str(path) for path in policy.output_roots],
        "allowed_gpu_names": list(policy.allowed_gpu_names),
        "min_free_memory_mib": policy.min_free_memory_mib,
        "sync_timeout_sec": policy.sync_timeout_sec,
    }


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise PolicyApprovalError(f"approved policy store is invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise PolicyApprovalError("approved policy store must be a JSON object")
    return raw


def _write_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def approve_policy(
    policy: GpuMcpPolicy,
    *,
    store_path: str | Path | None = None,
    diff_summary: list[str] | None = None,
    approved_by: str = "human",
) -> dict[str, Any]:
    store = Path(store_path).expanduser() if store_path is not None else default_store_path()
    store = store.resolve()
    config_path = str(policy.config_path.resolve())
    current_hash = policy_file_hash(policy.config_path)
    data = _load_store(store)
    entry = data.get(config_path)
    if not isinstance(entry, dict):
        entry = {"history": []}
    history = entry.get("history")
    if not isinstance(history, list):
        history = []

    event = {
        "hash": current_hash,
        "approved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "approved_by": approved_by,
        "summary": policy_summary(policy),
        "diff_summary": list(diff_summary or []),
    }
    history.append(event)
    entry = {
        "current_hash": current_hash,
        "history": history,
    }
    data[config_path] = entry
    _write_store(store, data)
    return entry


def verify_policy_approved(
    config_path: str | Path,
    *,
    store_path: str | Path | None = None,
) -> dict[str, Any]:
    config = Path(config_path).expanduser().resolve()
    store = Path(store_path).expanduser() if store_path is not None else default_store_path()
    store = store.resolve()
    data = _load_store(store)
    entry = data.get(str(config))
    if not isinstance(entry, dict):
        raise PolicyApprovalError(f"policy is not approved: {config}")
    current_hash = policy_file_hash(config)
    approved_hash = entry.get("current_hash")
    if approved_hash != current_hash:
        raise PolicyApprovalError(f"policy changed since approval: {config}")
    return entry


def diff_policy_summary(old: GpuMcpPolicy, new: GpuMcpPolicy) -> list[str]:
    old_summary = policy_summary(old)
    new_summary = policy_summary(new)
    diffs: list[str] = []
    for key in (
        "nodes",
        "script_roots",
        "write_roots",
        "output_roots",
        "allowed_gpu_names",
    ):
        old_values = set(old_summary[key])
        new_values = set(new_summary[key])
        for item in sorted(new_values - old_values):
            diffs.append(f"added {key[:-1] if key.endswith('s') else key} {item}")
        for item in sorted(old_values - new_values):
            diffs.append(f"removed {key[:-1] if key.endswith('s') else key} {item}")
    for key in ("min_free_memory_mib", "sync_timeout_sec"):
        if old_summary[key] != new_summary[key]:
            diffs.append(f"changed {key} from {old_summary[key]} to {new_summary[key]}")
    return diffs or ["no safety-relevant policy changes"]


def load_and_verify_policy(
    config_path: str | Path,
    *,
    store_path: str | Path | None = None,
) -> GpuMcpPolicy:
    policy = load_policy(config_path)
    verify_policy_approved(policy.config_path, store_path=store_path)
    return policy
