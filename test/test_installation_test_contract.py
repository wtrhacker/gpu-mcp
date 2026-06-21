from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy


def installation_module():
    try:
        return importlib.import_module("installation_test")
    except ModuleNotFoundError:
        pytest.fail("installation_test.py is missing")


def _write_target_repo(repo: Path, mcp_root: Path, *, host: str = "gpu-a") -> Path:
    (repo / ".codex").mkdir(parents=True, exist_ok=True)
    (repo / "jobs").mkdir(parents=True, exist_ok=True)
    (repo / "results").mkdir(parents=True, exist_ok=True)
    (repo / ".gpu_mcp_logs").mkdir(parents=True, exist_ok=True)
    config = repo / "gpu-mcp.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                f"nodes = [{host!r}]",
                "script_roots = ['jobs']",
                "write_roots = ['results']",
                "output_roots = ['.gpu_mcp_logs']",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                "sync_timeout_sec = 30",
                "",
            ]
        )
    )
    (repo / ".codex" / "config.toml").write_text(
        "\n".join(
            [
                "[mcp_servers.gpu-cluster-mcp]",
                f"command = {sys.executable!r}",
                "args = [",
                f"  {str(mcp_root / 'gpu_mcp_server.py')!r},",
                '  "--config",',
                f"  {str(config)!r},",
                "]",
                f"cwd = {str(repo)!r}",
                "enabled = true",
                "startup_timeout_sec = 20",
                "tool_timeout_sec = 60",
                "",
                "[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.check_gpus]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.check_gpu_processes]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.cluster_info]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.manage_gpu_job]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.list_gpu_reservations]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.preview_policy_reload]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.reject_policy_reload]",
                'approval_mode = "approve"',
                "[mcp_servers.gpu-cluster-mcp.tools.reload_policy]",
                'approval_mode = "prompt"',
                "",
            ]
        )
    )
    return config


def _write_mcp_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "gpu_mcp_server.py",
        "gpu_mcp_doctor.py",
        "gpu_mcp_policy_hook.py",
    ):
        (root / name).write_text("# installed file\n")


def _approve(config: Path, store: Path) -> None:
    approve_policy(
        load_policy(config),
        store_path=store,
        diff_summary=["installation test fixture approval"],
        approved_by="pytest",
    )


def _by_name(result: dict) -> dict[str, dict]:
    return {check["name"]: check for check in result["checks"]}


def test_default_installation_check_runs_codex_probe_and_reports_ready(
    tmp_path,
):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    _write_mcp_root(mcp_root)
    config = _write_target_repo(repo, mcp_root)
    _approve(config, store)

    def fake_runner(argv, **kwargs):
        if "--output-last-message" not in argv:
            return module.CommandResult(0, "imports ok", "")
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"status": "ok", "visible": True}))
        return module.CommandResult(0, "", "")

    result = module.run_installation_check(
        repo=repo,
        mcp_root=mcp_root,
        python_executable=sys.executable,
        approval_store=store,
        command_runner=fake_runner,
    )

    assert result["status"] == "pass"
    assert result["readiness"] == "ready"
    assert result["proof_level"] == "codex_exec"
    checks = _by_name(result)
    assert checks["install_root_files"]["status"] == "pass"
    assert checks["policy_approval"]["status"] == "pass"
    assert checks["repo_local_codex_config"]["status"] == "pass"
    assert checks["codex_mcp_probe"]["status"] == "pass"


def test_static_installation_check_blocks_unapproved_policy(tmp_path):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    _write_mcp_root(mcp_root)
    _write_target_repo(repo, mcp_root)

    result = module.run_installation_check(
        repo=repo,
        mcp_root=mcp_root,
        python_executable=sys.executable,
        approval_store=store,
        command_runner=lambda argv, **kwargs: module.CommandResult(0, "ok", ""),
    )

    assert result["status"] == "fail"
    assert result["readiness"] == "blocked"
    check = _by_name(result)["policy_approval"]
    assert check["status"] == "fail"
    assert "approve-policy" in check["next_action"]


def test_live_probes_do_not_run_when_static_checks_are_blocking(tmp_path):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    captured: list[list[str]] = []
    _write_mcp_root(mcp_root)
    _write_target_repo(repo, mcp_root)

    def fake_runner(argv, **kwargs):
        captured.append([str(item) for item in argv])
        if "--output-last-message" in argv:
            pytest.fail("live Codex probe should not run after a blocking static failure")
        return module.CommandResult(0, "imports ok", "")

    result = module.run_installation_check(
        repo=repo,
        mcp_root=mcp_root,
        python_executable=sys.executable,
        approval_store=store,
        live_gpu=True,
        command_runner=fake_runner,
    )

    assert result["status"] == "fail"
    assert result["readiness"] == "blocked"
    assert _by_name(result)["codex_mcp_probe"]["status"] == "skip"
    assert _by_name(result)["live_gpu_probe"]["status"] == "skip"


def test_codex_probe_uses_codex_exec_without_ignore_rules(tmp_path):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    captured: list[list[str]] = []
    _write_mcp_root(mcp_root)
    config = _write_target_repo(repo, mcp_root)
    _approve(config, store)

    def fake_runner(argv, **kwargs):
        captured.append([str(item) for item in argv])
        if "--output-last-message" not in argv:
            return module.CommandResult(0, "imports ok", "")
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"status": "ok", "visible": True}))
        return module.CommandResult(0, "", "")

    result = module.run_installation_check(
        repo=repo,
        mcp_root=mcp_root,
        python_executable=sys.executable,
        approval_store=store,
        command_runner=fake_runner,
    )

    assert result["status"] == "pass"
    assert result["readiness"] == "ready"
    assert result["proof_level"] == "codex_exec"
    argv = captured[-1]
    assert argv[:3] == ["codex", "--ask-for-approval", "never"]
    assert "exec" in argv
    assert "-C" in argv and argv[argv.index("-C") + 1] == str(repo)
    assert "--ignore-rules" not in argv
    assert "--output-last-message" in argv
    assert "gpu-cluster-mcp/check_gpus" in argv[-1]


def test_live_gpu_probe_is_explicit_and_writes_temporary_probe_under_script_root(
    tmp_path,
):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    captured: list[list[str]] = []
    _write_mcp_root(mcp_root)
    config = _write_target_repo(repo, mcp_root, host="gpu-a")
    _approve(config, store)

    def fake_runner(argv, **kwargs):
        captured.append([str(item) for item in argv])
        if "--output-last-message" not in argv:
            return module.CommandResult(0, "imports ok", "")
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "job_lifecycle": "succeeded",
                    "cuda_visible_devices": "0",
                    "used_gpu": True,
                }
            )
        )
        return module.CommandResult(0, "", "")

    result = module.run_installation_check(
        repo=repo,
        mcp_root=mcp_root,
        python_executable=sys.executable,
        approval_store=store,
        live_gpu=True,
        host="gpu-a",
        gpu_index=0,
        command_runner=fake_runner,
    )

    assert result["status"] == "pass"
    assert result["readiness"] == "ready"
    assert result["proof_level"] == "remote_gpu"
    checks = _by_name(result)
    assert checks["live_gpu_probe"]["status"] == "pass"
    probe = repo / "jobs" / ".gpu_mcp_installation_probe.py"
    assert probe.exists()
    assert "CUDA_VISIBLE_DEVICES" in probe.read_text()
    argv = captured[-1]
    assert "gpu-cluster-mcp/run_python_on_gpu" in argv[-1]
    assert "gpu-cluster-mcp/manage_gpu_job" in argv[-1]
    assert "script_path='jobs/.gpu_mcp_installation_probe.py'" in argv[-1]


def test_cli_prints_json_result(tmp_path, capsys):
    module = installation_module()
    mcp_root = tmp_path / "gpu-mcp"
    repo = tmp_path / "repo"
    store = tmp_path / "approved-policies.json"
    _write_mcp_root(mcp_root)
    config = _write_target_repo(repo, mcp_root)
    _approve(config, store)

    def fake_runner(argv, **kwargs):
        if "--output-last-message" not in argv:
            return module.CommandResult(0, "imports ok", "")
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"status": "ok", "visible": True}))
        return module.CommandResult(0, "", "")

    exit_code = module.main(
        [
            "--repo",
            str(repo),
            "--mcp-root",
            str(mcp_root),
            "--python",
            sys.executable,
            "--approval-store",
            str(store),
            "--json",
        ],
        command_runner=fake_runner,
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["target_repo"] == str(repo)
    assert result["readiness"] == "ready"
    assert result["proof_level"] == "codex_exec"
