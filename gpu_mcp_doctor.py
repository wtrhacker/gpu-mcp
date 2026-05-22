from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpu_mcp_config import ConfigError, load_policy


class DoctorError(ValueError):
    """Raised when install-readiness validation fails."""


MCP_SERVER_NAME = "gpu-cluster-mcp"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(name: str, status: str, message: str, *, host: str = "", details: dict | None = None) -> dict:
    check = {
        "name": name,
        "status": status,
        "host": host,
        "message": message,
    }
    if details:
        check["details"] = {
            key: value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
            for key, value in details.items()
        }
    return check


def _result(status: str, config_path: Path, repo_root: Path, checks: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "config_path": str(config_path),
        "repo_root": str(repo_root),
        "generated_at": _now(),
        "checks": checks,
    }


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gpu_mcp_doctor.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--config", required=True, type=Path)
    check.add_argument("--json", action="store_true")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        raise DoctorError("use `check --config /absolute/path/to/gpu-mcp.toml`; mcp-config is not a v1 command") from exc

    if args.command != "check":
        raise DoctorError("use the check command; mcp-config is not a v1 command")
    if not args.config.is_absolute():
        raise DoctorError("--config must be absolute")
    args.config = args.config.resolve()
    return args


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def validate_repo_local_codex_config(repo: str | Path, config_path: str | Path) -> dict:
    repo_path = Path(repo).resolve()
    expected_config = Path(config_path).resolve()
    codex_config = repo_path / ".codex" / "config.toml"
    if not codex_config.exists():
        raise DoctorError("repo-local Codex config is missing")
    raw = _load_toml(codex_config)
    servers = raw.get("mcp_servers", {})
    for name, server in servers.items():
        if name != MCP_SERVER_NAME:
            continue
        args = [str(arg) for arg in server.get("args", [])]
        if "--config" not in args:
            continue
        index = args.index("--config")
        try:
            actual_config = Path(args[index + 1]).resolve()
        except IndexError as exc:
            raise DoctorError("repo-local MCP config has --config without value") from exc
        if actual_config != expected_config:
            raise DoctorError("stale repo-local MCP config points at wrong repo/config")
        tool = server.get("tools", {}).get("run_python_on_gpu", {})
        if tool.get("approval_mode") != "approve":
            raise DoctorError("run_python_on_gpu approval_mode must be approve")
        return {
            "status": "ok",
            "server_name": name,
            "config_path": str(actual_config),
            "tool_timeout_sec": int(server.get("tool_timeout_sec", 0)),
        }
    raise DoctorError(f"repo-local Codex config must register {MCP_SERVER_NAME} with --config")


def run_codex_mcp_probe(
    *,
    repo: str | Path,
    tool_name: str,
    expected_repo_root: str | Path,
    codex_runner: dict | None = None,
) -> dict:
    repo_path = Path(repo).resolve()
    expected = Path(expected_repo_root).resolve()
    if codex_runner is not None:
        result = codex_runner.get("exec_result", {})
    else:
        output_path = repo_path / ".gpu_mcp_doctor_probe.txt"
        prompt = (
            "Do not run shell commands. Use the MCP tool "
            f"{tool_name} with host='gpu-a' and script_path='jobs/ok_job.py'. "
            "Report the exact tool result."
        )
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "-C",
                str(repo_path),
                "--sandbox",
                "workspace-write",
                "--output-last-message",
                str(output_path),
                prompt,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise DoctorError(f"codex exec probe failed: {completed.stdout}")
        result = json.loads(output_path.read_text())

    if Path(str(result.get("repo_root", ""))).resolve() != expected:
        raise DoctorError("codex exec probe returned wrong repo")
    return {
        "status": "ok",
        "evidence_source": "codex_exec",
        "repo_root": str(expected),
        "config_path": str(result.get("config_path", "")),
    }


def check_timeout_alignment(mcp_tool_timeout_sec: int, sync_timeout_sec: int) -> dict:
    if mcp_tool_timeout_sec <= sync_timeout_sec:
        raise DoctorError("tool_timeout_sec must be greater than sync_timeout_sec")
    return {
        "status": "ok",
        "message": "tool_timeout_sec exceeds sync_timeout_sec",
        "tool_timeout_sec": mcp_tool_timeout_sec,
        "sync_timeout_sec": sync_timeout_sec,
    }


def run_checks(
    *,
    config_path: str | Path,
    codex_project_dir: str | Path,
    checks: list[str] | None = None,
    remote_probe: dict[str, Any] | None = None,
) -> dict:
    config = Path(config_path).expanduser()
    repo = Path(codex_project_dir).expanduser().resolve()
    selected = set(checks or ["config", "repo_local_codex_config", "timeout"])
    emitted: list[dict] = []

    try:
        policy = load_policy(config)
        emitted.append(_check("config", "pass", "config parsed"))
    except ConfigError as exc:
        emitted.append(_check("config", "fail", str(exc)))
        fallback_repo = repo
        return _result("fail", config, fallback_repo, emitted)

    if "remote_environment" in selected:
        probe = remote_probe or {}
        if (
            probe.get("realpath") == str(policy.repo_root)
            and probe.get("python")
            and probe.get("nvidia_smi") == "ok"
            and probe.get("minimal_imports") == "ok"
        ):
            emitted.append(_check("remote_environment", "pass", "remote environment probe passed"))
        else:
            emitted.append(_check("remote_environment", "fail", "remote environment probe failed"))

    return _result(
        "pass" if all(check["status"] != "fail" for check in emitted) else "fail",
        policy.config_path,
        policy.repo_root,
        emitted,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(sys.argv[1:] if argv is None else argv)
    result = run_checks(config_path=args.config, codex_project_dir=args.config.parent)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"GPU MCP doctor: {result['status']}")
        for check in result["checks"]:
            print(f"- {check['name']}: {check['status']} - {check['message']}")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
