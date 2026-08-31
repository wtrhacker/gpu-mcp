from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from gpu_mcp_config import ConfigError, GpuMcpPolicy, load_policy
from gpu_mcp_doctor import DoctorError, MCP_SERVER_NAME, validate_repo_local_codex_config
from gpu_mcp_policy_approval import (
    PolicyApprovalError,
    policy_file_hash,
    verify_policy_approved,
)


DEFAULT_MCP_ROOT = Path("/home/tingran/gpu-mcp")
DEFAULT_PYTHON = Path("/home/tingran/miniconda3/bin/python")
REQUIRED_ROOT_FILES = (
    "gpu_mcp_server.py",
    "gpu_mcp_doctor.py",
    "gpu_mcp_policy_hook.py",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


CommandRunner = Callable[..., CommandResult]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_command(argv: list[str], **kwargs) -> CommandResult:
    completed = subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        **kwargs,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _check(
    name: str,
    status: str,
    message: str,
    *,
    severity: str = "blocking",
    evidence_source: str = "local",
    next_action: str = "",
    host: str = "",
    details: dict | None = None,
) -> dict:
    check = {
        "name": name,
        "status": status,
        "severity": severity,
        "host": host,
        "message": message,
        "evidence_source": evidence_source,
        "next_action": next_action,
    }
    if details:
        check["details"] = {
            key: value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
            for key, value in details.items()
        }
    return check


def _details_from_error(error: BaseException) -> dict:
    return {"error": str(error)}


def _load_codex_config(repo: Path) -> dict:
    config = repo / ".codex" / "config.toml"
    with config.open("rb") as fh:
        return tomllib.load(fh)


def _validate_server_path(repo: Path, mcp_root: Path, config_path: Path) -> dict:
    raw = _load_codex_config(repo)
    server = raw.get("mcp_servers", {}).get(MCP_SERVER_NAME)
    if not isinstance(server, dict):
        raise ValueError(f"{MCP_SERVER_NAME} missing from repo-local Codex config")
    args = [str(arg) for arg in server.get("args", [])]
    expected_server = (mcp_root / "gpu_mcp_server.py").resolve()
    if str(expected_server) not in args:
        raise ValueError(f"{MCP_SERVER_NAME} must point at {expected_server}")
    if "--config" not in args:
        raise ValueError(f"{MCP_SERVER_NAME} args must include --config")
    actual_config = Path(args[args.index("--config") + 1]).resolve()
    if actual_config != config_path.resolve():
        raise ValueError(f"{MCP_SERVER_NAME} --config points at {actual_config}")
    return {
        "server_path": str(expected_server),
        "config_path": str(actual_config),
    }


def _relative_to_repo(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _first_repo_root(roots: Iterable[Path], repo: Path) -> Path:
    for root in roots:
        try:
            root.resolve().relative_to(repo.resolve())
        except ValueError:
            continue
        root.mkdir(parents=True, exist_ok=True)
        return root
    raise ValueError("no repo-local root is available for the live GPU probe")


def _write_live_gpu_probe(policy: GpuMcpPolicy) -> tuple[str, str]:
    script_root = _first_repo_root(policy.script_roots, policy.repo_root)
    output_root = _first_repo_root(policy.output_roots, policy.repo_root)
    script = script_root / ".gpu_mcp_installation_probe.py"
    output = output_root / "gpu_mcp_installation_probe.log"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import os

            print("CUDA_VISIBLE_DEVICES=" + str(os.environ.get("CUDA_VISIBLE_DEVICES")))
            used_gpu = False

            try:
                import jax

                used_gpu = any(device.platform == "gpu" for device in jax.devices())
            except Exception:
                try:
                    import torch

                    used_gpu = bool(torch.cuda.is_available())
                except Exception:
                    used_gpu = False

            print("used_gpu=" + str(used_gpu))
            """
        ).lstrip()
    )
    return _relative_to_repo(policy.repo_root, script), _relative_to_repo(policy.repo_root, output)


def _read_probe_json(output_path: Path) -> dict:
    try:
        raw = output_path.read_text()
    except FileNotFoundError as exc:
        raise ValueError("codex probe did not write an output message") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"codex probe output was not JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("codex probe output must be a JSON object")
    return parsed


def _run_codex_exec_probe(
    *,
    repo: Path,
    prompt: str,
    command_runner: CommandRunner,
    timeout: int = 180,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="gpu_mcp_installation_test_") as tmp:
        output_path = Path(tmp) / "codex-output.json"
        argv = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            str(repo),
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(output_path),
            prompt,
        ]
        completed = command_runner(argv, timeout=timeout)
        if completed.returncode != 0:
            raise ValueError((completed.stdout or completed.stderr or "codex exec failed").strip())
        return _read_probe_json(output_path)


def _codex_visibility_prompt() -> str:
    return (
        "Do not run shell commands. Do not edit files. Use only the MCP tool "
        f"{MCP_SERVER_NAME}/check_gpus with samples=1 and threshold=10. "
        "Then report only JSON with keys status and tool_result. Set status to "
        '"ok" if the MCP tool call returned normally.'
    )


def _live_gpu_prompt(host: str, gpu_index: int, script_path: str, output_file: str) -> str:
    return (
        "Do not run shell commands. Do not edit files. Use only these MCP tools: "
        f"{MCP_SERVER_NAME}/run_python_on_gpu and {MCP_SERVER_NAME}/manage_gpu_job. "
        f"First call {MCP_SERVER_NAME}/run_python_on_gpu with "
        f"host={host!r}, gpu_index={gpu_index}, script_path={script_path!r}, "
        f"args=[], output_file={output_file!r}, job_role='smoke', "
        "expected_duration_sec=60, cadence_hint_sec=60. Then call "
        f"{MCP_SERVER_NAME}/manage_gpu_job with action='status', the returned "
        "job_id, and early_poll_reason='installation test live GPU probe'. "
        "Report only JSON with keys status, job_lifecycle, cuda_visible_devices, "
        "and used_gpu."
    )


def run_installation_check(
    *,
    repo: str | Path,
    mcp_root: str | Path = DEFAULT_MCP_ROOT,
    python_executable: str | Path = DEFAULT_PYTHON,
    approval_store: str | Path | None = None,
    live_gpu: bool = False,
    host: str | None = None,
    gpu_index: int = 0,
    command_runner: CommandRunner = _run_command,
) -> dict:
    repo_path = Path(repo).expanduser().resolve()
    mcp_root_path = Path(mcp_root).expanduser().resolve()
    python_path = Path(python_executable).expanduser()
    config_path = repo_path / "gpu-mcp.toml"
    checks: list[dict] = []
    policy: GpuMcpPolicy | None = None

    missing = [name for name in REQUIRED_ROOT_FILES if not (mcp_root_path / name).is_file()]
    if missing:
        checks.append(
            _check(
                "install_root_files",
                "fail",
                "shared MCP install is missing required files",
                next_action=f"repair or resync {mcp_root_path}",
                details={"missing": ", ".join(missing), "mcp_root": str(mcp_root_path)},
            )
        )
    else:
        checks.append(
            _check(
                "install_root_files",
                "pass",
                "shared MCP install files are present",
                details={"mcp_root": str(mcp_root_path)},
            )
        )

    import_probe = command_runner(
        [
            str(python_path),
            "-c",
            "import mcp.server.fastmcp; import paramiko; import fabric",
        ],
        timeout=30,
    )
    if import_probe.returncode == 0:
        checks.append(
            _check(
                "python_imports",
                "pass",
                "server Python can import MCP dependencies",
                details={"python": str(python_path)},
            )
        )
    else:
        checks.append(
            _check(
                "python_imports",
                "fail",
                "server Python cannot import MCP dependencies",
                next_action=f"install gpu-mcp dependencies for {python_path}",
                details={"stdout": import_probe.stdout, "stderr": import_probe.stderr},
            )
        )

    try:
        policy = load_policy(config_path)
        checks.append(
            _check(
                "policy_config",
                "pass",
                "repo policy parsed",
                details={"config_path": str(policy.config_path), "nodes": ",".join(policy.nodes)},
            )
        )
    except ConfigError as exc:
        checks.append(
            _check(
                "policy_config",
                "fail",
                "repo policy is invalid",
                next_action=f"fix {config_path}",
                details=_details_from_error(exc),
            )
        )

    if policy is not None:
        try:
            approval = verify_policy_approved(policy.config_path, store_path=approval_store)
            checks.append(
                _check(
                    "policy_approval",
                    "pass",
                    "policy is approved and fresh",
                    details={"policy_hash": approval.get("current_hash", policy_file_hash(policy.config_path))},
                )
            )
        except PolicyApprovalError as exc:
            checks.append(
                _check(
                    "policy_approval",
                    "fail",
                    "policy is awaiting initial activation or is stale",
                    next_action=(
                        "start or restart Codex from the trusted repo, call "
                        "preview_policy_reload, show the complete raw preview to the human, "
                        "then call reload_policy only after explicit approval"
                    ),
                    details=_details_from_error(exc),
                )
            )

        try:
            codex_info = validate_repo_local_codex_config(repo_path, policy.config_path)
            server_info = _validate_server_path(repo_path, mcp_root_path, policy.config_path)
            codex_info.update(server_info)
            checks.append(
                _check(
                    "repo_local_codex_config",
                    "pass",
                    "repo-local Codex config points at this MCP install and policy",
                    evidence_source="toml",
                    details=codex_info,
                )
            )
        except (DoctorError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            checks.append(
                _check(
                    "repo_local_codex_config",
                    "fail",
                    "repo-local Codex config is not usable",
                    evidence_source="toml",
                    next_action=f"fix {repo_path / '.codex' / 'config.toml'}",
                    details=_details_from_error(exc),
                )
            )

    static_failed = any(check["status"] == "fail" for check in checks)

    if static_failed:
        checks.append(
            _check(
                "codex_mcp_probe",
                "skip",
                "live Codex MCP probe was not run because static checks failed",
                severity="info",
                evidence_source="codex_exec",
                next_action="fix blocking static checks, then rerun the install test",
            )
        )
    else:
        try:
            result = _run_codex_exec_probe(
                repo=repo_path,
                prompt=_codex_visibility_prompt(),
                command_runner=command_runner,
            )
            if result.get("status") != "ok":
                raise ValueError(f"codex MCP probe did not report ok: {result}")
            checks.append(
                _check(
                    "codex_mcp_probe",
                    "pass",
                    "fresh Codex session can call the repo-local MCP server",
                    evidence_source="codex_exec",
                    details={"result_status": str(result.get("status"))},
                )
            )
        except ValueError as exc:
            checks.append(
                _check(
                    "codex_mcp_probe",
                    "fail",
                    "fresh Codex MCP probe failed",
                    evidence_source="codex_exec",
                    next_action=f"restart Codex from {repo_path} after fixing repo-local MCP config",
                    details=_details_from_error(exc),
                )
            )

    if static_failed:
        checks.append(
            _check(
                "live_gpu_probe",
                "skip",
                "live GPU launch probe was not run because static checks failed",
                severity="info",
                evidence_source="mcp_tool",
                next_action="fix blocking static checks, then rerun with --live-gpu",
            )
        )
    elif live_gpu:
        try:
            if policy is None:
                raise ValueError("valid policy is required for live GPU probe")
            selected_host = host or policy.nodes[0]
            if selected_host not in policy.nodes:
                raise ValueError(f"host is not allowed by repo policy: {selected_host}")
            script_path, output_file = _write_live_gpu_probe(policy)
            result = _run_codex_exec_probe(
                repo=repo_path,
                prompt=_live_gpu_prompt(selected_host, gpu_index, script_path, output_file),
                command_runner=command_runner,
                timeout=300,
            )
            if result.get("status") != "ok":
                raise ValueError(f"live GPU probe did not report ok: {result}")
            if result.get("job_lifecycle") != "succeeded":
                raise ValueError(f"live GPU job did not succeed: {result}")
            if str(result.get("cuda_visible_devices")) != str(gpu_index):
                raise ValueError(f"CUDA_VISIBLE_DEVICES did not match gpu_index: {result}")
            if result.get("used_gpu") is not True:
                raise ValueError(f"live probe did not prove GPU framework use: {result}")
            checks.append(
                _check(
                    "live_gpu_probe",
                    "pass",
                    "MCP launched a tiny GPU probe on a non-local host",
                    evidence_source="mcp_tool",
                    host=selected_host,
                    details={"gpu_index": gpu_index, "script_path": script_path, "output_file": output_file},
                )
            )
        except ValueError as exc:
            checks.append(
                _check(
                    "live_gpu_probe",
                    "fail",
                    "live GPU launch probe failed",
                    evidence_source="mcp_tool",
                    host=host or "",
                    next_action="fix the reported issue, or rerun without --live-gpu if only static readiness is needed",
                    details=_details_from_error(exc),
                )
            )
    else:
        checks.append(
            _check(
                "live_gpu_probe",
                "skip",
                "live GPU launch probe was not requested",
                severity="info",
                evidence_source="mcp_tool",
                next_action="rerun with --live-gpu to prove real remote GPU launch",
            )
        )

    failed = any(check["status"] == "fail" for check in checks)
    status = "fail" if failed else "pass"
    if failed:
        readiness = "blocked"
    elif live_gpu:
        readiness = "ready"
    else:
        readiness = "ready"
    proof_level = "remote_gpu" if live_gpu and not failed else "codex_exec" if not failed else "static"

    return {
        "schema_version": 1,
        "status": status,
        "readiness": readiness,
        "proof_level": proof_level,
        "target_repo": str(repo_path),
        "config_path": str(config_path),
        "mcp_install_root": str(mcp_root_path),
        "server_name": MCP_SERVER_NAME,
        "server_path": str(mcp_root_path / "gpu_mcp_server.py"),
        "generated_at": _now(),
        "checks": checks,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="installation_test.py")
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--mcp-root", default=DEFAULT_MCP_ROOT, type=Path)
    parser.add_argument("--python", default=DEFAULT_PYTHON, type=Path)
    parser.add_argument("--approval-store", default=None, type=Path)
    parser.add_argument("--live-gpu", action="store_true")
    parser.add_argument("--host", default=None)
    parser.add_argument("--gpu-index", default=0, type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.repo.is_absolute():
        parser.error("--repo must be absolute")
    return args


def _print_human(result: dict) -> None:
    label = "READY" if result["readiness"] == "ready" else "INCOMPLETE" if result["status"] == "pass" else "BLOCKED"
    print(f"GPU MCP install test: {label}")
    print(f"Target repo: {result['target_repo']}")
    print(f"Proof level: {result['proof_level']}")
    for check in result["checks"]:
        print(f"- {check['name']}: {check['status']} - {check['message']}")
    next_actions = [check["next_action"] for check in result["checks"] if check.get("next_action")]
    if next_actions:
        print("")
        print("Next action:")
        print(f"  {next_actions[0]}")


def main(argv: list[str] | None = None, *, command_runner: CommandRunner = _run_command) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    result = run_installation_check(
        repo=args.repo,
        mcp_root=args.mcp_root,
        python_executable=args.python,
        approval_store=args.approval_store,
        live_gpu=args.live_gpu,
        host=args.host,
        gpu_index=args.gpu_index,
        command_runner=command_runner,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
