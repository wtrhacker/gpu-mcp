from __future__ import annotations

"""Executable contract for the AI-facing install doctor.

Doctor should be an install-readiness checker, not a second implementation of
GPU MCP policy. It validates static setup, runs a small Codex/MCP probe, and
reports structured evidence the installer agent can parse.

Expected future API:

- `gpu_mcp_doctor.DoctorError`
- `gpu_mcp_doctor.parse_cli_args(argv) -> argparse.Namespace`
- `gpu_mcp_doctor.run_checks(config_path, codex_project_dir, ...) -> dict`
- `gpu_mcp_doctor.check_timeout_alignment(mcp_tool_timeout_sec, sync_timeout_sec)`
- `gpu_mcp_doctor.validate_repo_local_codex_config(repo, config_path)`
- `gpu_mcp_doctor.run_codex_mcp_probe(repo, tool_name, expected_repo_root, ...)`
"""

import importlib
import json
import uuid
import subprocess
from pathlib import Path

import jsonschema
import pytest


pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos" / "doctor_contract"
DOCTOR_RESULT_SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "schemas" / "doctor-result.schema.json").read_text()
)


@pytest.fixture()
def doctor_module():
    return importlib.import_module("gpu_mcp_doctor")


@pytest.fixture()
def repo_fixture(request):
    repo = FIXTURE_ROOT / f"{request.node.name}-{uuid.uuid4().hex}" / "repo"
    (repo / ".codex").mkdir(parents=True, exist_ok=True)
    (repo / "jobs").mkdir(parents=True, exist_ok=True)
    (repo / "results").mkdir(parents=True, exist_ok=True)
    return repo


def _write_policy(repo: Path, *, sync_timeout_sec: int = 5) -> Path:
    config = repo / "gpu-mcp.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                "nodes = ['gpu-a']",
                "script_roots = ['jobs']",
                "write_roots = ['results']",
                "output_roots = ['.gpu_mcp_logs']",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                f"sync_timeout_sec = {sync_timeout_sec}",
                "",
            ]
        )
    )
    return config


def _write_codex_config(
    repo: Path,
    config_path: Path,
    *,
    approval_mode: str = "approve",
    reload_approval_mode: str = "prompt",
) -> Path:
    codex_config = repo / ".codex" / "config.toml"
    codex_config.write_text(
        "\n".join(
            [
                "[mcp_servers.gpu-cluster-mcp]",
                'command = "/usr/bin/python3"',
                "args = [",
                '  "/opt/gpu-mcp/gpu_mcp_server.py",',
                '  "--config",',
                f"  {str(config_path)!r},",
                "]",
                "enabled = true",
                "startup_timeout_sec = 20",
                "tool_timeout_sec = 10",
                "",
                "[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]",
                f"approval_mode = {approval_mode!r}",
                "[mcp_servers.gpu-cluster-mcp.tools.reload_policy]",
                f"approval_mode = {reload_approval_mode!r}",
                "[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]",
                f"approval_mode = {approval_mode!r}",
                "",
            ]
        )
    )
    return codex_config


def _assert_doctor_result(result: dict, *, status: str) -> None:
    jsonschema.validate(instance=result, schema=DOCTOR_RESULT_SCHEMA)
    assert result["schema_version"] == 1
    assert result["status"] == status
    assert isinstance(result["checks"], list)


def test_doctor_rejects_malformed_or_missing_policy_file(doctor_module, repo_fixture):
    missing = repo_fixture / "gpu-mcp.toml"

    result = doctor_module.run_checks(
        config_path=missing,
        codex_project_dir=repo_fixture,
        checks=["config"],
    )

    _assert_doctor_result(result, status="fail")
    assert any(check["name"] == "config" for check in result["checks"])


def test_doctor_cli_has_one_check_command_with_absolute_config(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)

    args = doctor_module.parse_cli_args(["check", "--config", str(config), "--json"])

    assert args.command == "check"
    assert args.config == config
    assert args.json is True


def test_doctor_cli_can_parse_explicit_policy_approval(doctor_module, repo_fixture):
    config = _write_policy(repo_fixture)

    args = doctor_module.parse_cli_args(
        ["approve-policy", "--config", str(config), "--store", str(repo_fixture / "approved.json"), "--yes", "--json"]
    )

    assert args.command == "approve-policy"
    assert args.config == config
    assert args.yes is True


def test_doctor_approve_policy_writes_external_approval_record(
    doctor_module, repo_fixture, tmp_path, capsys
):
    config = _write_policy(repo_fixture)
    store = tmp_path / "approved-policies.json"

    exit_code = doctor_module.main(
        ["approve-policy", "--config", str(config), "--store", str(store), "--yes", "--json"]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "approved"
    assert result["config_path"] == str(config)
    stored = json.loads(store.read_text())
    assert stored[str(config)]["current_hash"] == result["policy_hash"]
    assert stored[str(config)]["history"][-1]["approved_by"] == "human"


def test_doctor_main_uses_provided_cli_arguments(doctor_module, repo_fixture, capsys):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)

    exit_code = doctor_module.main(["check", "--config", str(config), "--json"])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    _assert_doctor_result(result, status="pass")


def test_doctor_cli_rejects_old_mcp_config_subcommand(doctor_module):
    with pytest.raises(doctor_module.DoctorError, match="check|mcp-config"):
        doctor_module.parse_cli_args(["mcp-config", "--client", "codex"])


def test_doctor_validates_repo_local_codex_config_points_to_policy(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)

    result = doctor_module.validate_repo_local_codex_config(repo_fixture, config)

    assert result["status"] == "ok"
    assert result["config_path"] == str(config)
    assert result["server_name"] == "gpu-cluster-mcp"


def test_doctor_rejects_repo_local_config_with_relative_config_path(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    codex_config = _write_codex_config(repo_fixture, config)
    codex_config.write_text(codex_config.read_text().replace(str(config), "gpu-mcp.toml"))

    with pytest.raises(doctor_module.DoctorError, match="absolute"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_rejects_global_or_stale_fixed_repo_mcp_config(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    stale_repo = repo_fixture.parent / "other_repo"
    stale_config = stale_repo / "gpu-mcp.toml"
    stale_repo.mkdir(parents=True)
    _write_codex_config(repo_fixture, stale_config)

    with pytest.raises(doctor_module.DoctorError, match="stale|repo"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_requires_stable_gpu_cluster_mcp_server_name(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    codex_config = _write_codex_config(repo_fixture, config)
    codex_config.write_text(codex_config.read_text().replace("gpu-cluster-mcp", "repo-a-gpu-probe"))

    with pytest.raises(doctor_module.DoctorError, match="gpu-cluster-mcp"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_requires_approve_mode_for_noninteractive_codex_probe(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config, approval_mode="auto")

    with pytest.raises(doctor_module.DoctorError, match="approval_mode"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_requires_prompt_mode_for_policy_reload_and_approve_mode_for_kill(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    codex_config = _write_codex_config(repo_fixture, config)
    codex_config.write_text(
        codex_config.read_text().replace(
            "[mcp_servers.gpu-cluster-mcp.tools.reload_policy]\napproval_mode = 'prompt'",
            "[mcp_servers.gpu-cluster-mcp.tools.reload_policy]\napproval_mode = 'auto'",
        )
    )

    with pytest.raises(doctor_module.DoctorError, match="reload_policy approval_mode"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)

    _write_codex_config(repo_fixture, config)
    codex_config.write_text(
        codex_config.read_text().replace(
            "[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]\napproval_mode = 'approve'",
            "[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]\napproval_mode = 'auto'",
        )
    )

    with pytest.raises(doctor_module.DoctorError, match="kill_gpu_process approval_mode"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_rejects_non_integer_tool_timeout(doctor_module, repo_fixture):
    config = _write_policy(repo_fixture)
    codex_config = _write_codex_config(repo_fixture, config)
    codex_config.write_text(codex_config.read_text().replace("tool_timeout_sec = 10", "tool_timeout_sec = true"))

    with pytest.raises(doctor_module.DoctorError, match="tool_timeout_sec"):
        doctor_module.validate_repo_local_codex_config(repo_fixture, config)


def test_doctor_uses_codex_exec_probe_not_codex_mcp_list(doctor_module, repo_fixture):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)

    result = doctor_module.run_codex_mcp_probe(
        repo=repo_fixture,
        tool_name="gpu-cluster-mcp/run_python_on_gpu",
        expected_repo_root=repo_fixture,
        codex_runner={
            "mcp_list": "Name gpu-cluster /old/global/path",
            "exec_result": {
                "status": "ok",
                "repo_root": str(repo_fixture),
                "config_path": str(config),
            },
        },
    )

    assert result["status"] == "ok"
    assert result["evidence_source"] == "codex_exec"
    assert result["repo_root"] == str(repo_fixture)


def test_doctor_codex_probe_uses_noninteractive_rules_enabled_argv(
    doctor_module, repo_fixture, monkeypatch
):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "repo_root": str(repo_fixture),
                    "config_path": str(config),
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(doctor_module.subprocess, "run", fake_run)

    result = doctor_module.run_codex_mcp_probe(
        repo=repo_fixture,
        tool_name="gpu-cluster-mcp/run_python_on_gpu",
        expected_repo_root=repo_fixture,
    )

    assert result["status"] == "ok"
    assert captured["argv"][:3] == ["codex", "--ask-for-approval", "never"]
    assert "--ignore-rules" not in captured["argv"]


def test_doctor_probe_refuses_preexisting_symlink_output(doctor_module, repo_fixture):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)
    probe_output = repo_fixture / ".gpu_mcp_doctor_probe.txt"
    probe_output.symlink_to(repo_fixture / "outside.txt")

    with pytest.raises(doctor_module.DoctorError, match="probe output"):
        doctor_module.run_codex_mcp_probe(
            repo=repo_fixture,
            tool_name="gpu-cluster-mcp/run_python_on_gpu",
            expected_repo_root=repo_fixture,
        )


def test_doctor_rejects_probe_when_codex_exec_returns_wrong_repo(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)
    other_repo = repo_fixture.parent / "other_repo"

    with pytest.raises(doctor_module.DoctorError, match="repo"):
        doctor_module.run_codex_mcp_probe(
            repo=repo_fixture,
            tool_name="gpu-cluster-mcp/run_python_on_gpu",
            expected_repo_root=repo_fixture,
            codex_runner={
                "exec_result": {
                    "status": "ok",
                    "repo_root": str(other_repo),
                    "config_path": str(other_repo / "gpu-mcp.toml"),
                },
            },
        )


def test_doctor_checks_client_timeout_exceeds_server_timeout(doctor_module):
    result = doctor_module.check_timeout_alignment(
        mcp_tool_timeout_sec=10,
        sync_timeout_sec=5,
    )

    assert result["status"] == "ok"


def test_doctor_rejects_client_timeout_that_hides_server_timeout(doctor_module):
    with pytest.raises(doctor_module.DoctorError, match="tool_timeout_sec"):
        doctor_module.check_timeout_alignment(
            mcp_tool_timeout_sec=5,
            sync_timeout_sec=5,
        )


def test_doctor_remote_probe_is_readiness_only(doctor_module, repo_fixture):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)

    result = doctor_module.run_checks(
        config_path=config,
        codex_project_dir=repo_fixture,
        checks=["remote_environment"],
        remote_probe={
            "realpath": str(repo_fixture),
            "python": "/usr/bin/python3",
            "nvidia_smi": "ok",
            "minimal_imports": "ok",
        },
    )

    _assert_doctor_result(result, status="pass")
    assert all("policy_decision" not in check for check in result["checks"])


def test_doctor_check_fails_when_repo_local_codex_config_is_missing(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)

    result = doctor_module.run_checks(
        config_path=config,
        codex_project_dir=repo_fixture,
    )

    _assert_doctor_result(result, status="fail")
    assert any(check["name"] == "repo_local_codex_config" for check in result["checks"])


def test_doctor_check_fails_when_client_timeout_hides_server_timeout(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture, sync_timeout_sec=10)
    _write_codex_config(repo_fixture, config)

    result = doctor_module.run_checks(
        config_path=config,
        codex_project_dir=repo_fixture,
    )

    _assert_doctor_result(result, status="fail")
    assert any(check["name"] == "timeout" for check in result["checks"])


def test_doctor_check_reports_live_readiness_checks_as_skipped_by_default(
    doctor_module, repo_fixture
):
    config = _write_policy(repo_fixture)
    _write_codex_config(repo_fixture, config)

    result = doctor_module.run_checks(
        config_path=config,
        codex_project_dir=repo_fixture,
    )

    _assert_doctor_result(result, status="pass")
    skipped = {check["name"]: check for check in result["checks"] if check["status"] == "skip"}
    assert "remote_environment" in skipped
    assert "codex_mcp_probe" in skipped
    assert "raw_remote_command_policy" in skipped
