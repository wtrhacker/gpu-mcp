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
from gpu_mcp_policy_approval import approve_policy, policy_file_hash, policy_summary


class DoctorError(ValueError):
    """Raised when install-readiness validation fails."""


MCP_SERVER_NAME = "gpu-cluster-mcp"
REQUIRED_TOOL_APPROVAL_MODES = {
    "run_python_on_gpu": "approve",
    # Codex shows this prompt only in the human UI. The agent receives the
    # normal MCP result after approval and cannot observe the approval event.
    "reload_policy": "prompt",
    "kill_gpu_process": "approve",
}


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


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DoctorError(f"{name} must be a positive integer")
    if value <= 0:
        raise DoctorError(f"{name} must be a positive integer")
    return value


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
    approve = subparsers.add_parser("approve-policy")
    approve.add_argument("--config", required=True, type=Path)
    approve.add_argument("--store", type=Path, default=None)
    approve.add_argument("--yes", action="store_true")
    approve.add_argument("--json", action="store_true")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        raise DoctorError("use `check --config /absolute/path/to/gpu-mcp.toml`; mcp-config is not a v1 command") from exc

    if args.command not in {"check", "approve-policy"}:
        raise DoctorError("use the check command; mcp-config is not a v1 command")
    if not args.config.is_absolute():
        raise DoctorError("--config must be absolute")
    args.config = args.config.resolve()
    if getattr(args, "store", None) is not None:
        args.store = args.store.expanduser().resolve()
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
            raw_config_arg = Path(args[index + 1])
        except IndexError as exc:
            raise DoctorError("repo-local MCP config has --config without value") from exc
        if not raw_config_arg.is_absolute():
            raise DoctorError("repo-local MCP --config path must be absolute")
        actual_config = raw_config_arg.resolve()
        if actual_config != expected_config:
            raise DoctorError("stale repo-local MCP config points at wrong repo/config")
        tools = server.get("tools", {})
        for tool_name, expected_mode in REQUIRED_TOOL_APPROVAL_MODES.items():
            tool = tools.get(tool_name, {})
            if tool.get("approval_mode") != expected_mode:
                raise DoctorError(f"{tool_name} approval_mode must be {expected_mode}")
        tool_timeout_sec = _positive_int(
            server.get("tool_timeout_sec", 0),
            name="tool_timeout_sec",
        )
        return {
            "status": "ok",
            "server_name": name,
            "config_path": str(actual_config),
            "tool_timeout_sec": tool_timeout_sec,
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
        if output_path.exists() or output_path.is_symlink():
            raise DoctorError("probe output path already exists or is a symlink")
        prompt = (
            "Do not run shell commands. Use the MCP tool "
            f"{tool_name} with host='gpu-a' and script_path='jobs/ok_job.py'. "
            "Report the exact tool result."
        )
        completed = subprocess.run(
            [
                "codex",
                "--ask-for-approval",
                "never",
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
    if checks is None:
        selected.update({"remote_environment", "codex_mcp_probe", "raw_remote_command_policy"})
    emitted: list[dict] = []

    try:
        policy = load_policy(config)
        emitted.append(_check("config", "pass", "config parsed"))
    except ConfigError as exc:
        emitted.append(_check("config", "fail", str(exc)))
        fallback_repo = repo
        return _result("fail", config, fallback_repo, emitted)

    if "remote_environment" in selected:
        if remote_probe is None:
            emitted.append(_check("remote_environment", "skip", "live remote probe not requested"))
        elif (
            remote_probe.get("realpath") == str(policy.repo_root)
            and remote_probe.get("python")
            and remote_probe.get("nvidia_smi") == "ok"
            and remote_probe.get("minimal_imports") == "ok"
        ):
            emitted.append(_check("remote_environment", "pass", "remote environment probe passed"))
        else:
            emitted.append(_check("remote_environment", "fail", "remote environment probe failed"))

    if "codex_mcp_probe" in selected:
        emitted.append(_check("codex_mcp_probe", "skip", "live codex exec MCP probe not requested"))

    if "raw_remote_command_policy" in selected:
        emitted.append(_check("raw_remote_command_policy", "skip", "live raw remote-command probes not requested"))

    repo_config_info: dict[str, Any] | None = None
    if "repo_local_codex_config" in selected:
        try:
            repo_config_info = validate_repo_local_codex_config(repo, policy.config_path)
            emitted.append(
                _check(
                    "repo_local_codex_config",
                    "pass",
                    "repo-local Codex MCP config points at this policy",
                    details=repo_config_info,
                )
            )
        except DoctorError as exc:
            emitted.append(_check("repo_local_codex_config", "fail", str(exc)))

    if "timeout" in selected:
        try:
            if repo_config_info is None:
                repo_config_info = validate_repo_local_codex_config(repo, policy.config_path)
            alignment = check_timeout_alignment(
                _positive_int(
                    repo_config_info.get("tool_timeout_sec", 0),
                    name="tool_timeout_sec",
                ),
                policy.sync_timeout_sec,
            )
            emitted.append(_check("timeout", "pass", alignment["message"], details=alignment))
        except DoctorError as exc:
            emitted.append(_check("timeout", "fail", str(exc)))

    return _result(
        "pass" if all(check["status"] != "fail" for check in emitted) else "fail",
        policy.config_path,
        policy.repo_root,
        emitted,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(sys.argv[1:] if argv is None else argv)
    if args.command == "approve-policy":
        if not args.yes:
            raise DoctorError("approve-policy requires --yes after human review")
        policy = load_policy(args.config)
        record = approve_policy(
            policy,
            store_path=args.store,
            diff_summary=["approved by doctor approve-policy"],
        )
        result = {
            "schema_version": 1,
            "status": "approved",
            "config_path": str(policy.config_path),
            "repo_root": str(policy.repo_root),
            "policy_hash": policy_file_hash(policy.config_path),
            "summary": policy_summary(policy),
            "record": record,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"GPU MCP policy approved: {result['config_path']}")
            print(f"Hash: {result['policy_hash']}")
        return 0

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
