from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

from gpu_mcp_config import GpuMcpPolicy


class PolicyError(ValueError):
    """Raised when a requested GPU MCP action violates repo policy."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _result(status: str, code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    flat_details: dict[str, str | int | float | bool | None] = {}
    for key, value in (details or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            flat_details[key] = value
        else:
            flat_details[key] = str(value)
    return {
        "schema_version": 1,
        "status": status,
        "code": code,
        "message": message,
        "details": flat_details,
    }


def validate_host(policy: GpuMcpPolicy, host: str) -> str:
    if host not in policy.nodes:
        raise PolicyError(f"host not allowed: {host}")
    return host


def resolve_script_path(policy: GpuMcpPolicy, script_path: str | Path) -> Path:
    requested = Path(script_path).expanduser()
    if not requested.is_absolute():
        requested = policy.repo_root / requested
    resolved = requested.resolve()
    if not any(_inside(resolved, root) for root in policy.script_roots):
        raise PolicyError(f"script outside approved script roots: {script_path}")
    if resolved.suffix != ".py":
        raise PolicyError("script must be a .py file")
    return resolved


def validate_write_path(
    policy: GpuMcpPolicy,
    path: str | Path,
    *,
    purpose: str = "write",
) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = policy.repo_root / requested
    resolved = requested.resolve()
    if not any(_inside(resolved, root) for root in policy.write_roots):
        raise PolicyError(f"{purpose} path outside approved write roots: {path}")
    return resolved


def validate_output_path(policy: GpuMcpPolicy, path: str | Path) -> Path:
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        requested = policy.repo_root / requested
    resolved = requested.resolve()
    if not any(_inside(resolved, root) for root in policy.output_roots):
        raise PolicyError(f"output path outside approved output roots: {path}")
    return resolved


def scan_python_script_safety(path: str | Path) -> list[str]:
    source_path = Path(path)
    try:
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]

    issues: list[str] = []
    forbidden_imports = {"subprocess", "socket"}
    forbidden_calls = {
        ("os", "system"),
        ("os", "popen"),
        ("subprocess", "run"),
        ("subprocess", "Popen"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("socket", "create_connection"),
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in forbidden_imports:
                    issues.append(f"forbidden import: {top}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".", 1)[0]
            if top in forbidden_imports:
                issues.append(f"forbidden import: {top}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                issues.append("dynamic import via __import__ is not allowed")
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                pair = (node.func.value.id, node.func.attr)
                if pair in forbidden_calls:
                    issues.append(f"forbidden call: {pair[0]}.{pair[1]}")
    return issues


def run_python_on_gpu(
    policy: GpuMcpPolicy,
    host: str,
    script_path: str | Path,
    *args: str,
    async_mode: bool = False,
    output_file: str | Path | None = None,
) -> dict:
    validate_host(policy, host)
    script = resolve_script_path(policy, script_path)
    issues = scan_python_script_safety(script)
    if issues:
        joined = "; ".join(issues)
        if any("socket" in issue for issue in issues):
            return _result("error", "policy_violation", joined, {"script_path": str(script)})
        raise PolicyError(joined)

    if async_mode:
        if output_file is None:
            raise PolicyError("async output_file is required")
        validate_output_path(policy, output_file)
        return _result("ok", "async_launched", "async launch accepted", {"host": host})

    try:
        completed = subprocess.run(
            [sys.executable, str(script), *args],
            cwd=policy.repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=policy.sync_timeout_sec,
            check=False,
        )
    except TimeoutExpired:
        return _result(
            "error",
            "timeout",
            f"script exceeded sync_timeout_sec={policy.sync_timeout_sec}",
            {"host": host, "script_path": str(script), "timeout_sec": policy.sync_timeout_sec},
        )
    except Exception as exc:  # pragma: no cover - defensive result shape.
        return _result("error", "execution_error", str(exc), {"host": host, "script_path": str(script)})

    status = "ok" if completed.returncode == 0 else "error"
    code = "success" if completed.returncode == 0 else "execution_failed"
    return _result(
        status,
        code,
        "script completed" if completed.returncode == 0 else "script failed",
        {
            "host": host,
            "script_path": str(script),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )


TimeoutExpired = subprocess.TimeoutExpired
