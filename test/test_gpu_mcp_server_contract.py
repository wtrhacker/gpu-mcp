from __future__ import annotations

"""Contract tests for real server startup and safe-run boundaries."""

import os
import ast
import importlib
import json
import shlex
import subprocess
import sys
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "gpu_mcp_server.py"
SAFE_RUNNER = REPO_ROOT / "gpu_mcp_safe_runner.py"
GUARD = REPO_ROOT / "gpu_mcp_guard.py"
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos" / "server_contract"


@pytest.fixture()
def repo_fixture(request):
    repo = FIXTURE_ROOT / f"{request.node.name}-{uuid.uuid4().hex}" / "repo"
    (repo / "jobs").mkdir(parents=True, exist_ok=True)
    (repo / "results").mkdir(parents=True, exist_ok=True)
    (repo / ".gpu_mcp_logs").mkdir(parents=True, exist_ok=True)
    return repo


def _write_config(repo: Path) -> Path:
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
                "sync_timeout_sec = 5",
                "",
            ]
        )
    )
    return config


def _import_server_with_config(monkeypatch, config: Path):
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--config", str(config)])
    old_server = sys.modules.get("gpu_mcp_server")
    if old_server is not None and hasattr(old_server, "HEARTBEAT_MANAGER"):
        old_server.HEARTBEAT_MANAGER.stop()
    sys.modules.pop("gpu_mcp_server", None)
    return importlib.import_module("gpu_mcp_server")


def _server_subprocess_env(registry: Path) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if not existing_pythonpath
        else str(REPO_ROOT) + os.pathsep + existing_pythonpath
    )
    env["GPU_MCP_TEST_RESERVATION_ROOT"] = str(registry)
    env["GPU_MCP_TEST_DISABLE_POLICY_APPROVAL"] = "1"
    env.setdefault("PYTEST_CURRENT_TEST", "gpu-mcp subprocess contract")
    return env


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _seed_phase7_smoke_record(
    server,
    repo: Path,
    *,
    job_role: str = "smoke",
    terminal_status: str | None = "success",
    runtime_sec: int = 120,
) -> dict:
    script = repo / "jobs" / f"smoke_{uuid.uuid4().hex[:8]}.py"
    script.write_text("print('smoke')\n")
    job_id = server.reservations.generate_job_id(suffix=f"smoke-{uuid.uuid4().hex[:8]}")
    attempt_id = server.reservations.generate_attempt_id(suffix=f"smoke-{uuid.uuid4().hex[:8]}")
    reservation_key = server.reservations.reservation_key("gpu-a", 0)
    started = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=runtime_sec)
    record = server.reservations.build_job_record(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key,
        host="gpu-a",
        gpu_index=0,
        script_path=script,
        args=["--small"],
        output_file=repo / ".gpu_mcp_logs" / f"{job_id}.log",
        server_instance_id=server.SERVER_INSTANCE_ID,
        next_poll_after=_rfc3339(ended + timedelta(seconds=60)),
        created_at=_rfc3339(started),
    )
    record.update(
        {
            "job_role": job_role,
            "job_role_defaulted": False,
            "heartbeat_interval_sec": 60,
            "job_lifecycle": (
                "running"
                if terminal_status is None
                else "succeeded" if terminal_status == "success" else "failed"
            ),
            "cadence_basis": {
                "source": "conservative_no_evidence",
                "selected_interval_sec": 60,
            },
        }
    )
    server.reservations.atomic_write_json(server.reservations.job_record_path(repo, job_id), record)
    if terminal_status is not None:
        outcome = server.reservations.build_outcome_record(
            job_id=job_id,
            attempt_id=attempt_id,
            reservation_key_value=reservation_key,
            host="gpu-a",
            gpu_index=0,
            terminal_status=terminal_status,
            started_at=_rfc3339(started),
            ended_at=_rfc3339(ended),
            remote_pid=12345,
            exit_code=0 if terminal_status == "success" else 1,
        )
        server.reservations.atomic_write_json(
            server.reservations.outcome_record_path(repo, job_id, attempt_id),
            outcome,
        )
    return record


def _assert_phase7_private_fields_not_shared(metadata: dict) -> None:
    private_fields = {
        "job_role",
        "job_role_defaulted",
        "smoke_job_id",
        "smoke_cadence_representative",
        "smoke_skip_reason",
        "expected_duration_sec",
        "cadence_hint_sec",
        "cadence_basis",
        "cadence_update_reason",
        "last_early_poll_reason",
        "last_early_poll_at",
    }
    leaked = private_fields.intersection(metadata)
    assert leaked == set()


def _launch_phase7_setup_job(
    server,
    *,
    host: str,
    gpu_index: int,
    script_path: str,
    output_file: str,
) -> dict:
    kwargs = {
        "host": host,
        "gpu_index": gpu_index,
        "script_path": script_path,
        "output_file": output_file,
    }
    try:
        return json.loads(server.run_python_on_gpu(**kwargs, job_role="one_off"))
    except TypeError as exc:
        if "job_role" not in str(exc):
            raise
        return json.loads(server.run_python_on_gpu(**kwargs))


def test_server_refuses_to_start_without_explicit_config():
    completed = subprocess.run(
        [sys.executable, str(SERVER)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 2
    assert "--config" in completed.stdout


def test_server_ignores_gpu_mcp_config_environment_fallback(repo_fixture):
    config = _write_config(repo_fixture)
    env = os.environ.copy()
    env["GPU_MCP_CONFIG"] = str(config)

    completed = subprocess.run(
        [sys.executable, str(SERVER)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 2
    assert "--config" in completed.stdout


def test_safe_run_requires_explicit_config(repo_fixture):
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")

    completed = subprocess.run(
        [sys.executable, str(SERVER), "--safe-run", str(script)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 2
    assert "--config" in completed.stdout


def test_safe_run_blocks_dir_fd_write_escape(repo_fixture):
    config = _write_config(repo_fixture)
    outside = repo_fixture.parent / "outside"
    outside.mkdir()
    script = repo_fixture / "jobs" / "dir_fd_escape.py"
    script.write_text(
        "\n".join(
            [
                "import os",
                f"fd = os.open({str(outside)!r}, os.O_RDONLY)",
                "try:",
                "    os.open('escaped.txt', os.O_WRONLY | os.O_CREAT, dir_fd=fd)",
                "finally:",
                "    os.close(fd)",
                "",
            ]
        )
    )

    completed = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--safe-run", str(script)],
        cwd=repo_fixture,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    assert "dir_fd" in completed.stdout or "outside approved roots" in completed.stdout
    assert not (outside / "escaped.txt").exists()


def test_run_python_rejects_bool_gpu_index(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    result = server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=True,
        script_path="jobs/does_not_matter.py",
    )

    parsed = json.loads(result)
    assert parsed["status"] == "refused"
    assert "gpu_index" in parsed["reason"]
    assert "integer" in parsed["reason"]


def test_run_python_rejects_non_list_args_and_non_bool_async(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    (repo_fixture / "jobs" / "ok.py").write_text("print('ok')\n")
    server = _import_server_with_config(monkeypatch, config)

    args_result = server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        args="--not-a-list",
    )
    async_result = server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        async_mode="false",
    )

    assert json.loads(args_result)["status"] == "refused"
    assert "args" in json.loads(args_result)["reason"]
    assert json.loads(async_result)["status"] == "refused"
    assert "async_mode" in json.loads(async_result)["reason"]


def test_phase0_managed_job_status_shape_has_no_target(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    result = json.loads(server.manage_gpu_job(action="status"))

    assert result["status"] == "no_target"
    assert result["action"] == "status"
    assert result["job_id"] is None
    assert result["reservation_key"] is None
    assert result["owned_by_current_server"] is False
    assert result["allowed_actions"] == ["status"]
    assert result["server_instance_id"].startswith("server-")


def test_phase0_list_reservations_shape_and_fresh_semantics(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)

    result = json.loads(server.list_gpu_reservations(scope="mine", fresh=True))

    assert result["status"] == "ok"
    assert result["scope"] == "mine"
    assert result["fresh"] is True
    assert result["fresh_semantics"] == "bounded_refresh_not_filter"
    assert result["registry_root"] == str(registry.resolve())
    assert result["registry_status"] == "ok"
    assert result["reservations"] == []


def test_phase0_list_reservations_reports_sanitized_rows(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    key = "gpu-a.gpu0"
    reservation_dir = registry / key
    reservation_dir.mkdir(parents=True)
    metadata = {
        "schema_version": 1,
        "job_id": "job-20260530T123456Z-abc123",
        "attempt_id": "attempt-20260530T123456Z-def456",
        "reservation_key": key,
        "host": "gpu-a",
        "gpu_index": 0,
        "repo": str(repo_fixture.resolve()),
        "script_name": "train.py",
        "owner_user": server.GPU_MCP_USER,
        "server_instance_id": "server-login-1234-srv",
        "remote_pid": 12345,
        "remote_start_time": None,
        "remote_boot_id": None,
        "process_fingerprint": "gpu-mcp-process:fingerprint",
        "reserved_at": "2026-05-30T12:00:00Z",
        "last_heartbeat_at": "2026-05-30T12:00:00Z",
        "heartbeat_interval_sec": 600,
    }
    (reservation_dir / "metadata.json").write_text(json.dumps(metadata))

    result = json.loads(server.list_gpu_reservations(scope="mine", fresh=False))

    assert len(result["reservations"]) == 1
    row = result["reservations"][0]
    assert row["job_id"] == "job-20260530T123456Z-abc123"
    assert row["reservation_key"] == key
    assert row["host"] == "gpu-a"
    assert row["gpu_index"] == 0
    assert row["script_name"] == "train.py"
    assert row["reservation_state"] in {"RESERVED", "STALE_RESERVED"}
    assert row["computed_state"] == row["reservation_state"]
    assert row["metadata_status"] == "ok"
    assert row["server_instance_id"] == "server-login-1234-srv"
    assert row["owned_by_current_server"] is False
    assert row["allowed_actions"] == ["status"]
    assert row["last_inspection"] is None
    assert "script_path" not in json.dumps(result)
    assert "output_file" not in json.dumps(result)
    assert "args" not in json.dumps(result)


def test_phase0_list_reservations_rejects_symlink_and_malformed_metadata(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    registry.mkdir()
    (registry / "gpu-a.gpu0").symlink_to(tmp_path)
    malformed = registry / "gpu-a.gpu1"
    malformed.mkdir()
    (malformed / "metadata.json").write_text("[]")

    result = json.loads(server.list_gpu_reservations(scope="all", fresh=False))

    rows = {row["reservation_key"]: row for row in result["reservations"]}
    assert rows["gpu-a.gpu0"]["reservation_state"] == "UNKNOWN_RESERVED"
    assert rows["gpu-a.gpu1"]["reservation_state"] == "UNKNOWN_RESERVED"
    assert rows["gpu-a.gpu1"]["job_id"] is None
    assert rows["gpu-a.gpu1"]["script_name"] is None


def test_phase0_manage_rejects_noncanonical_reservation_key(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    result = json.loads(server.manage_gpu_job(action="status", reservation_key=".."))

    assert result["status"] == "refused"
    assert "reservation_key" in result["reason"]


def test_phase0_check_gpus_returns_structured_registry_overlay(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        assert "query-gpu=index,name" in cmd
        return "0, NVIDIA RTX 4090, 0, 1, 24576\n"

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["registry_status"] == "ok"
    assert result["gpus"][0]["host"] == "gpu-a"
    assert result["gpus"][0]["gpu_index"] == 0
    assert result["gpus"][0]["availability"] == "available"
    assert result["gpus"][0]["reservation_key"] == "gpu-a.gpu0"


def test_phase0_check_gpus_fails_closed_when_registry_unavailable(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry_file = tmp_path / "not-a-directory"
    registry_file.write_text("nope")
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry_file))
    server = _import_server_with_config(monkeypatch, config)

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        return "0, NVIDIA RTX 4090, 0, 1, 24576\n"

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["registry_status"] == "unavailable"
    assert result["gpus"][0]["availability"] == "unknown_unavailable"


def test_phase0_check_gpus_fails_closed_when_registry_iterdir_fails(
    repo_fixture,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    class BrokenRegistry:
        def exists(self):
            return True

        def iterdir(self):
            raise OSError("simulated registry listing failure")

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        return "0, NVIDIA RTX 4090, 0, 1, 24576\n"

    monkeypatch.setattr(server.reservations, "reservation_registry_root", lambda: BrokenRegistry())
    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["status"] == "error"
    assert result["registry_status"] == "unavailable"
    assert "simulated registry listing failure" in result["registry_error"]
    assert result["gpus"][0]["availability"] == "unknown_unavailable"


def test_phase0_run_python_returns_managed_handle_for_sync_compat(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    captured = {}

    def fake_ssh(host, cmd):
        captured["cmd"] = cmd
        return "12345\n"

    monkeypatch.setattr(server, "_ssh_run", fake_ssh)

    result = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        async_mode=False,
        output_file=".gpu_mcp_logs/job.log",
    ))

    assert result["status"] == "launched"
    assert result["job_id"].startswith("job-")
    assert result["attempt_id"].startswith("attempt-")
    assert result["reservation_key"] == "gpu-a.gpu0"
    assert result["host"] == "gpu-a"
    assert result["gpu_index"] == 0
    assert result["server_instance_id"].startswith("server-")
    assert result["process"]["remote_pid"] == 12345
    assert "GPU_MCP_PROCESS_FINGERPRINT" in captured["cmd"]
    assert result["process"]["process_fingerprint"] in captured["cmd"]
    assert result["output"]["path"] == str(repo_fixture / ".gpu_mcp_logs" / "job.log")
    assert result["next_poll_after"]
    assert result["async_mode_requested"] is False
    metadata = json.loads((repo_fixture / ".gpu_mcp_state" / "jobs" / result["job_id"] / "job.json").read_text())
    assert metadata["reservation_key"] == "gpu-a.gpu0"
    reservation_metadata = json.loads(
        (registry / "gpu-a.gpu0" / "metadata.json").read_text()
    )
    assert reservation_metadata["job_id"] == result["job_id"]


def test_phase0_two_repos_share_registry_but_not_job_state(tmp_path, monkeypatch):
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    for repo in (repo_a, repo_b):
        (repo / "jobs").mkdir(parents=True)
        (repo / "results").mkdir()
        (repo / ".gpu_mcp_logs").mkdir()
        (repo / "jobs" / "ok.py").write_text("print('ok')\n")
    config_a = _write_config(repo_a)
    config_b = _write_config(repo_b)
    registry = tmp_path / "shared_reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))

    server_a = _import_server_with_config(monkeypatch, config_a)
    monkeypatch.setattr(server_a, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server_a, "_ssh_run", lambda host, cmd: "12345\n")
    launch = json.loads(server_a.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/a.log",
    ))

    assert launch["status"] == "launched"
    assert (registry / "gpu-a.gpu0" / "metadata.json").exists()
    assert (repo_a / ".gpu_mcp_state" / "jobs" / launch["job_id"] / "job.json").exists()
    assert not (repo_b / ".gpu_mcp_state" / "jobs").exists()

    server_b = _import_server_with_config(monkeypatch, config_b)
    listed = json.loads(server_b.list_gpu_reservations(scope="all", fresh=False))

    assert listed["registry_root"] == str(registry.resolve())
    assert listed["reservations"][0]["job_id"] == launch["job_id"]
    assert listed["reservations"][0]["reservation_key"] == "gpu-a.gpu0"
    assert listed["reservations"][0]["owned_by_current_server"] is False


def test_phase1_second_launch_same_gpu_is_refused_and_visible_in_check(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", "23456\n"])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))

    first = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/one.log",
    ))
    second = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/two.log",
    ))

    assert first["status"] == "launched"
    assert second["status"] == "refused"
    assert second["reservation_key"] == "gpu-a.gpu0"
    assert "already exists" in second["reason"]

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        return "0, NVIDIA RTX 4090, 0, 1, 24576\n"

    monkeypatch.setattr(server, "_host_run", fake_host_run)
    checked = json.loads(server.check_gpus(samples=1, threshold=10))

    assert checked["gpus"][0]["availability"] == "reserved"
    assert checked["gpus"][0]["reservation"]["job_id"] == first["job_id"]


def test_phase1_local_launch_failure_removes_prelaunch_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: True)

    def fail_popen(*args, **kwargs):
        raise OSError("local spawn failed before process exists")

    monkeypatch.setattr(server.subprocess, "Popen", fail_popen)

    result = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/fail.log",
    ))

    assert result["status"] == "refused"
    assert "local spawn failed" in result["reason"]
    assert not (registry / "gpu-a.gpu0").exists()


def test_phase2_heartbeat_once_updates_owned_metadata(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))

    assert launched["status"] == "launched"
    assert server.HEARTBEAT_MANAGER.owned_keys() == ["gpu-a.gpu0"]
    assert server.HEARTBEAT_MANAGER.heartbeat_once(
        "gpu-a.gpu0",
        now="2026-05-30T13:00:00Z",
    )

    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["last_heartbeat_at"] == "2026-05-30T13:00:00Z"
    assert metadata["heartbeat_interval_sec"] == 600


def test_phase2_status_works_while_heartbeat_unhealthy_and_mutations_refuse(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )
    server.HEARTBEAT_MANAGER.mark_unhealthy_for_tests("disk full")

    status = json.loads(server.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
    ))
    stop = json.loads(server.manage_gpu_job(action="stop", job_id=launched["job_id"]))
    launch = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=1,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job2.log",
    ))

    assert status["status"] == "ok"
    assert status["heartbeat_manager_healthy"] is False
    assert status["job_id"] == launched["job_id"]
    assert status["allowed_actions"] == ["status"]
    assert stop["status"] == "refused"
    assert "heartbeat manager is unhealthy" in stop["reason"]
    assert launch["status"] == "refused"
    assert "heartbeat manager is unhealthy" in launch["reason"]


def test_phase2_heartbeat_health_recovers_after_success(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))

    server.HEARTBEAT_MANAGER.force_write_failure_for_tests = True
    assert not server.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu0")
    assert not server.HEARTBEAT_MANAGER.is_healthy()
    server.HEARTBEAT_MANAGER.force_write_failure_for_tests = False
    assert server.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu0")
    assert server.HEARTBEAT_MANAGER.is_healthy()


def test_phase2_per_task_heartbeat_isolation(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", "12346\n"])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))

    first = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job0.log",
    ))
    second = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=1,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job1.log",
    ))

    assert first["status"] == "launched"
    assert second["status"] == "launched"
    assert server.HEARTBEAT_MANAGER.owned_keys() == ["gpu-a.gpu0", "gpu-a.gpu1"]
    assert server.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu0", now="2026-05-30T13:00:00Z")
    assert server.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu1", now="2026-05-30T13:05:00Z")
    assert json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())[
        "last_heartbeat_at"
    ] == "2026-05-30T13:00:00Z"
    assert json.loads((registry / "gpu-a.gpu1" / "metadata.json").read_text())[
        "last_heartbeat_at"
    ] == "2026-05-30T13:05:00Z"


def test_phase2_heartbeat_preserves_process_identity(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))

    assert server.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu0", now="2026-05-30T13:00:00Z")
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert metadata["remote_pid"] == 12345
    assert metadata["process_fingerprint"] == launched["process"]["process_fingerprint"]
    assert metadata["last_heartbeat_at"] == "2026-05-30T13:00:00Z"
    assert (registry / ".locks" / "gpu-a.gpu0.lock").exists()


def test_phase2_new_server_reports_but_does_not_adopt_old_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server_a = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server_a, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server_a, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server_a.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    old_server_id = launched["server_instance_id"]

    server_b = _import_server_with_config(monkeypatch, config)
    listed = json.loads(server_b.list_gpu_reservations(scope="mine", fresh=False))
    status = json.loads(server_b.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
    ))

    assert server_b.SERVER_INSTANCE_ID != old_server_id
    assert server_b.HEARTBEAT_MANAGER.owned_keys() == []
    assert not server_b.HEARTBEAT_MANAGER.heartbeat_once("gpu-a.gpu0")
    assert listed["reservations"][0]["owned_by_current_server"] is False
    assert status["status"] == "ok"
    assert status["owned_by_current_server"] is False
    assert status["allowed_actions"] == ["status"]


def _seed_reservation(
    registry: Path,
    repo: Path,
    *,
    host="gpu-a",
    gpu_index=0,
    remote_pid=12345,
    heartbeat="2020-01-01T00:00:00Z",
    remote_start_time=None,
    remote_boot_id=None,
    process_fingerprint="gpu-mcp-process:seeded",
):
    key = f"{host}.gpu{gpu_index}"
    reservation_dir = registry / key
    reservation_dir.mkdir(parents=True)
    metadata = {
        "schema_version": 1,
        "job_id": "job-20260530T123456Z-seeded",
        "attempt_id": "attempt-20260530T123456Z-seeded",
        "reservation_key": key,
        "host": host,
        "gpu_index": gpu_index,
        "repo": str(repo.resolve()),
        "script_name": "train.py",
        "owner_user": "tingran",
        "server_instance_id": "server-old-1234-seeded",
        "remote_pid": remote_pid,
        "remote_start_time": remote_start_time,
        "remote_boot_id": remote_boot_id,
        "process_fingerprint": process_fingerprint,
        "reserved_at": heartbeat,
        "last_heartbeat_at": heartbeat,
        "heartbeat_interval_sec": 60,
    }
    (reservation_dir / "metadata.json").write_text(json.dumps(metadata))
    return metadata


def _fake_one_gpu(server):
    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        return "0, NVIDIA RTX 4090, 0, 1, 24576\n"

    return fake_host_run


def test_phase3_stale_alive_process_remains_reserved(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture)
    monkeypatch.setattr(server, "_host_run", _fake_one_gpu(server))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "reserved"
    assert result["gpus"][0]["reservation_state"] == "STALE_RESERVED"
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_stale_gone_process_is_quarantined_and_gpu_available(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture)
    monkeypatch.setattr(server, "_host_run", _fake_one_gpu(server))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "available"
    assert not (registry / "gpu-a.gpu0").exists()
    assert list((registry / ".quarantine").glob("gpu-a.gpu0.*"))


def test_phase3_stale_unknown_inspection_stays_unavailable(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture)
    monkeypatch.setattr(server, "_host_run", _fake_one_gpu(server))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "unknown", "reason": "host unreachable", "remote_pid": 12345},
    )

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "reserved"
    assert result["gpus"][0]["reservation_state"] == "UNKNOWN_RESERVED"
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_stale_null_remote_pid_fails_closed(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture, remote_pid=None)
    monkeypatch.setattr(server, "_host_run", _fake_one_gpu(server))

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "reserved"
    assert result["gpus"][0]["reservation_state"] == "UNKNOWN_RESERVED"
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_launch_cleans_stale_gone_then_reacquires(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "54321\n")
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    result = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert result["status"] == "launched"
    assert result["process"]["remote_pid"] == 54321
    assert metadata["job_id"] == result["job_id"]
    assert list((registry / ".quarantine").glob("gpu-a.gpu0.*"))


def test_phase3_process_inspection_treats_start_boot_and_fingerprint_mismatch_as_gone(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "reservations"))
    server = _import_server_with_config(monkeypatch, config)
    metadata = _seed_reservation(
        tmp_path / "reservations",
        repo_fixture,
        remote_start_time="Thu May 30 12:00:00 2026",
        remote_boot_id="boot-a",
        process_fingerprint="gpu-mcp-process:expected",
    )

    def start_mismatch(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran S Thu May 30 12:01:00 2026"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", start_mismatch)
    assert server._inspect_reservation_process(metadata)["reason"] == "process start time mismatch"

    def boot_mismatch(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran S Thu May 30 12:00:00 2026"
        if cmd == "cat /proc/sys/kernel/random/boot_id":
            return "boot-b"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", boot_mismatch)
    assert server._inspect_reservation_process(metadata)["reason"] == "host boot id mismatch"

    metadata_no_boot = dict(metadata)
    metadata_no_boot["remote_boot_id"] = None

    def fingerprint_mismatch(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran S Thu May 30 12:00:00 2026"
        if "GPU_MCP_PROCESS_FINGERPRINT" in cmd:
            return "gpu-mcp-process:other"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fingerprint_mismatch)
    assert (
        server._inspect_reservation_process(metadata_no_boot)["reason"]
        == "process fingerprint mismatch"
    )


def test_phase3_process_inspection_positive_identity_path(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "reservations"))
    server = _import_server_with_config(monkeypatch, config)
    metadata = _seed_reservation(
        tmp_path / "reservations",
        repo_fixture,
        remote_start_time="Thu May 30 12:00:00 2026",
        remote_boot_id="boot-a",
        process_fingerprint="gpu-mcp-process:expected",
    )

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran S Thu May 30 12:00:00 2026"
        if cmd == "cat /proc/sys/kernel/random/boot_id":
            return "boot-a"
        if "GPU_MCP_PROCESS_FINGERPRINT" in cmd:
            return "gpu-mcp-process:expected"
        if "query-compute-apps" in cmd:
            return "12345, GPU-deadbeef, 42"
        if "query-gpu=index,uuid" in cmd:
            return "0, GPU-deadbeef"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fake_host_run)
    inspection = server._inspect_reservation_process(metadata)

    assert inspection["status"] == "alive"
    assert inspection["gpu_index"] == "0"


def test_phase3_matching_live_process_off_gpu_remains_reserved(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(
        registry,
        repo_fixture,
        remote_start_time="Thu May 30 12:00:00 2026",
        remote_boot_id="boot-a",
        process_fingerprint="gpu-mcp-process:expected",
    )

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if "query-gpu=index,name" in cmd:
            return "0, NVIDIA RTX 4090, 0, 1, 24576\n"
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran S Thu May 30 12:00:00 2026"
        if cmd == "cat /proc/sys/kernel/random/boot_id":
            return "boot-a"
        if "GPU_MCP_PROCESS_FINGERPRINT" in cmd:
            return "gpu-mcp-process:expected"
        if "query-compute-apps" in cmd:
            return ""
        if "query-gpu=index,uuid" in cmd:
            return "0, GPU-deadbeef"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    row = result["gpus"][0]
    assert row["availability"] == "reserved"
    assert row["reservation_state"] == "STALE_RESERVED"
    assert row["reservation"]["last_inspection"]["status"] == "alive"
    assert row["reservation"]["last_inspection"]["gpu_index"] is None
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_zombie_process_is_gone_for_cleanup(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(
        registry,
        repo_fixture,
        remote_start_time="Thu May 30 12:00:00 2026",
        process_fingerprint="gpu-mcp-process:expected",
    )

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if "query-gpu=index,name" in cmd:
            return "0, NVIDIA RTX 4090, 0, 1, 24576\n"
        if cmd.startswith("ps -p 12345"):
            return "12345 tingran Z Thu May 30 12:00:00 2026"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "available"
    assert not (registry / "gpu-a.gpu0").exists()
    assert list((registry / ".quarantine").glob("gpu-a.gpu0.*"))


def test_phase3_metadata_mismatch_fails_closed(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    metadata = _seed_reservation(registry, repo_fixture, host="gpu-b")
    mismatched_dir = registry / "gpu-a.gpu0"
    (registry / "gpu-b.gpu0").rename(mismatched_dir)
    metadata["reservation_key"] = "gpu-b.gpu0"
    (mismatched_dir / "metadata.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(server, "_host_run", _fake_one_gpu(server))

    result = json.loads(server.check_gpus(samples=1, threshold=10))

    assert result["gpus"][0]["availability"] == "reserved"
    assert result["gpus"][0]["reservation_state"] == "UNKNOWN_RESERVED"
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_stale_inspection_budget_skips_fail_closed(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    for index in range(6):
        _seed_reservation(registry, repo_fixture, gpu_index=index, remote_pid=12000 + index)
    calls = []

    def fake_inspect(metadata):
        calls.append(metadata["reservation_key"])
        return {"status": "alive", "reason": "still running", "remote_pid": metadata["remote_pid"]}

    monkeypatch.setattr(server, "_inspect_reservation_process", fake_inspect)
    rows = server._refresh_stale_reservations(server._load_reservation_rows(scope="all")[2])

    assert len(calls) == 2
    skipped = [row for row in rows.values() if row.get("last_inspection", {}).get("status") == "skipped"]
    assert len(skipped) == 4
    assert all(row["reservation_state"] == "UNKNOWN_RESERVED" for row in skipped)


def test_phase3_cleanup_re_read_aborts_on_heartbeat_refresh(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    stale_metadata = _seed_reservation(registry, repo_fixture)
    fresh_metadata = dict(stale_metadata)
    fresh_metadata["last_heartbeat_at"] = server.reservations.iso_timestamp()
    (registry / "gpu-a.gpu0" / "metadata.json").write_text(json.dumps(fresh_metadata))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    cleaned, inspection = server._cleanup_stale_gone_reservation("gpu-a.gpu0", stale_metadata)

    assert cleaned is False
    assert "heartbeat refreshed" in inspection["reason"]
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_cleanup_re_read_aborts_on_metadata_change(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    stale_metadata = _seed_reservation(registry, repo_fixture)
    changed_metadata = dict(stale_metadata)
    changed_metadata["remote_pid"] = 99999
    (registry / "gpu-a.gpu0" / "metadata.json").write_text(json.dumps(changed_metadata))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    cleaned, inspection = server._cleanup_stale_gone_reservation("gpu-a.gpu0", stale_metadata)

    assert cleaned is False
    assert "metadata changed" in inspection["reason"]
    assert (registry / "gpu-a.gpu0").exists()


def test_phase3_cleanup_guard_excludes_cross_process_heartbeat_refresh(
    repo_fixture,
    tmp_path,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    metadata = _seed_reservation(registry, repo_fixture)
    metadata_path = registry / "gpu-a.gpu0" / "metadata.json"
    ready_path = tmp_path / "heartbeat-ready"
    release_path = tmp_path / "release-heartbeat"
    cleanup_entered_path = tmp_path / "cleanup-entered"
    env = _server_subprocess_env(registry)

    heartbeat_code = textwrap.dedent(
        f"""
        import json
        import time
        from pathlib import Path

        import gpu_mcp_reservations as reservations

        registry = Path({str(registry)!r})
        metadata_path = Path({str(metadata_path)!r})
        ready_path = Path({str(ready_path)!r})
        release_path = Path({str(release_path)!r})

        with reservations.cleanup_finalization_guard(registry, "gpu-a.gpu0"):
            ready_path.write_text("locked")
            while not release_path.exists():
                time.sleep(0.01)
            metadata = json.loads(metadata_path.read_text())
            metadata["last_heartbeat_at"] = reservations.iso_timestamp()
            reservations.atomic_write_json(metadata_path, metadata)
        """
    )
    cleanup_code = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        sys.argv = [{str(SERVER)!r}, "--config", {str(config)!r}]
        import gpu_mcp_server as server

        server._inspect_reservation_process = lambda metadata: {{
            "status": "gone",
            "reason": "process exited",
            "remote_pid": metadata.get("remote_pid"),
        }}
        metadata = json.loads(Path({str(metadata_path)!r}).read_text())
        Path({str(cleanup_entered_path)!r}).write_text("entered")
        cleaned, inspection = server._cleanup_stale_gone_reservation("gpu-a.gpu0", metadata)
        print(json.dumps({{"cleaned": cleaned, "inspection": inspection}}, sort_keys=True))
        """
    )

    heartbeat_proc = subprocess.Popen(
        [sys.executable, "-c", heartbeat_code],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_path(ready_path)
    cleanup_proc = subprocess.Popen(
        [sys.executable, "-c", cleanup_code],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_path(cleanup_entered_path)
    time.sleep(0.2)
    completed_while_guard_held = cleanup_proc.poll() is not None
    release_path.write_text("go")
    heartbeat_stdout, heartbeat_stderr = heartbeat_proc.communicate(timeout=10)
    cleanup_stdout, cleanup_stderr = cleanup_proc.communicate(timeout=10)

    assert heartbeat_proc.returncode == 0, heartbeat_stderr or heartbeat_stdout
    assert cleanup_proc.returncode == 0, cleanup_stderr or cleanup_stdout
    assert completed_while_guard_held is False
    payload = json.loads(cleanup_stdout)
    assert payload["cleaned"] is False
    assert "heartbeat refreshed" in payload["inspection"]["reason"]
    assert (registry / "gpu-a.gpu0").exists()
    refreshed = json.loads(metadata_path.read_text())
    assert refreshed["last_heartbeat_at"] != metadata["last_heartbeat_at"]


def test_phase3_two_cleaners_racing_one_quarantines_one_aborts(
    repo_fixture,
    tmp_path,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    _seed_reservation(registry, repo_fixture)
    metadata_path = registry / "gpu-a.gpu0" / "metadata.json"
    ready_dir = tmp_path / "cleaner-ready"
    ready_dir.mkdir()
    start_path = tmp_path / "start-cleaners"
    env = _server_subprocess_env(registry)

    cleaner_code = textwrap.dedent(
        f"""
        import json
        import os
        import sys
        import time
        from pathlib import Path

        sys.argv = [{str(SERVER)!r}, "--config", {str(config)!r}]
        import gpu_mcp_server as server

        metadata = json.loads(Path({str(metadata_path)!r}).read_text())
        worker = os.environ["GPU_MCP_CLEANER_WORKER"]
        (Path({str(ready_dir)!r}) / worker).write_text("ready")
        start_path = Path({str(start_path)!r})
        while not start_path.exists():
            time.sleep(0.01)
        server._inspect_reservation_process = lambda metadata: {{
            "status": "gone",
            "reason": "process exited",
            "remote_pid": metadata.get("remote_pid"),
        }}
        cleaned, inspection = server._cleanup_stale_gone_reservation("gpu-a.gpu0", metadata)
        print(json.dumps({{"worker": worker, "cleaned": cleaned, "inspection": inspection}}, sort_keys=True))
        """
    )
    procs = []
    for index in range(2):
        child_env = env.copy()
        child_env["GPU_MCP_CLEANER_WORKER"] = str(index)
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", cleaner_code],
                cwd=REPO_ROOT,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    for index in range(2):
        _wait_for_path(ready_dir / str(index))
    start_path.write_text("go")
    outputs = [proc.communicate(timeout=10) for proc in procs]

    payloads = []
    for proc, (stdout, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, stderr or stdout
        payloads.append(json.loads(stdout))
    assert [payload["cleaned"] for payload in payloads].count(True) == 1
    assert [payload["cleaned"] for payload in payloads].count(False) == 1
    losing = next(payload for payload in payloads if not payload["cleaned"])
    assert losing["inspection"]["reason"] in {
        "reservation disappeared during cleanup",
        "reservation already gone",
    }
    assert not (registry / "gpu-a.gpu0").exists()
    assert len(list((registry / ".quarantine").glob("gpu-a.gpu0.*"))) == 1


def test_phase4_status_maps_terminal_outcome(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )
    outcome_path = server.reservations.outcome_record_path(
        repo_fixture,
        launched["job_id"],
        launched["attempt_id"],
    )
    server.reservations.atomic_write_json(outcome_path, server.reservations.build_outcome_record(
        job_id=launched["job_id"],
        attempt_id=launched["attempt_id"],
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        terminal_status="success",
        remote_pid=12345,
        exit_code=0,
        ended_at="2026-05-30T13:00:00Z",
    ))

    status = json.loads(server.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
    ))

    assert status["job_lifecycle"] == "succeeded"
    assert status["outcome"]["terminal_status"] == "success"
    assert status["next_poll_after"] is None


def test_phase4_managed_local_launch_writes_outcome_record(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "quick.py"
    script.write_text("print('quick done')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: True)

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/quick.py",
        output_file=".gpu_mcp_logs/quick.log",
    ))
    outcome_path = server.reservations.outcome_record_path(
        repo_fixture,
        launched["job_id"],
        launched["attempt_id"],
    )
    for _ in range(30):
        if outcome_path.exists():
            break
        time.sleep(0.1)

    outcome = json.loads(outcome_path.read_text())
    assert outcome["terminal_status"] == "success"
    assert outcome["exit_code"] == 0
    assert outcome["reservation_key"] == "gpu-a.gpu0"


def test_phase4_missing_outcome_reports_unknown(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
    ))

    assert status["job_lifecycle"] == "process_gone_unknown_outcome"
    assert status["outcome"] is None


def test_phase4_corrupt_outcome_reports_unknown(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        output_file=".gpu_mcp_logs/job.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )
    outcome_path = server.reservations.outcome_record_path(
        repo_fixture,
        launched["job_id"],
        launched["attempt_id"],
    )
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    outcome_path.write_text("{not valid json")

    status = json.loads(server.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
    ))

    assert status["job_lifecycle"] == "process_gone_unknown_outcome"
    assert status["outcome"] is None


def test_phase4_recovers_single_current_repo_reservation_without_job_id(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture, heartbeat=server.reservations.iso_timestamp())
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(action="status"))

    assert status["status"] == "ok"
    assert status["job_id"] == "job-20260530T123456Z-seeded"
    assert status["reservation_key"] == "gpu-a.gpu0"
    assert status["owned_by_current_server"] is False


def test_phase4_ambiguous_recovery_returns_candidates(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture, gpu_index=0, heartbeat=server.reservations.iso_timestamp())
    _seed_reservation(registry, repo_fixture, gpu_index=1, remote_pid=12346, heartbeat=server.reservations.iso_timestamp())
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": metadata["remote_pid"]},
    )

    status = json.loads(server.manage_gpu_job(action="status"))

    assert status["status"] == "ambiguous_target"
    assert len(status["candidates"]) == 2
    assert {row["reservation_key"] for row in status["candidates"]} == {"gpu-a.gpu0", "gpu-a.gpu1"}


def test_phase4_reservation_key_resolution_ignores_historical_local_job(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    stale_job = server.reservations.build_job_record(
        job_id="job-20260530T123456Z-oldjob",
        attempt_id="attempt-20260530T123456Z-oldattempt",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        script_path=repo_fixture / "jobs" / "old.py",
        args=[],
        output_file=repo_fixture / ".gpu_mcp_logs" / "old.log",
        server_instance_id="server-old-1111-local",
        next_poll_after="2026-05-30T13:00:00Z",
    )
    server.reservations.atomic_write_json(
        server.reservations.job_record_path(repo_fixture, "job-20260530T123456Z-oldjob"),
        stale_job,
    )
    _seed_reservation(registry, repo_fixture, heartbeat=server.reservations.iso_timestamp())
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(action="status", reservation_key="gpu-a.gpu0"))

    assert status["job_id"] == "job-20260530T123456Z-seeded"
    assert status["output"]["path"] is None
    assert not (
        repo_fixture
        / ".gpu_mcp_state"
        / "jobs"
        / "job-20260530T123456Z-seeded"
        / "job.json"
    ).exists()


def test_phase4_explicit_historical_job_id_does_not_bind_current_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    old_job_id = "job-20260530T123456Z-oldjob"
    old_record = server.reservations.build_job_record(
        job_id=old_job_id,
        attempt_id="attempt-20260530T123456Z-oldattempt",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        script_path=repo_fixture / "jobs" / "old.py",
        args=[],
        output_file=repo_fixture / ".gpu_mcp_logs" / "old.log",
        server_instance_id="server-old-1111-local",
        next_poll_after="2026-05-30T13:00:00Z",
    )
    server.reservations.atomic_write_json(
        server.reservations.job_record_path(repo_fixture, old_job_id),
        old_record,
    )
    _seed_reservation(registry, repo_fixture, heartbeat=server.reservations.iso_timestamp())
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: (_ for _ in ()).throw(AssertionError("must not inspect current reservation")),
    )

    status = json.loads(server.manage_gpu_job(action="status", job_id=old_job_id))
    reread = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / old_job_id / "job.json").read_text()
    )

    assert status["status"] == "ok"
    assert status["job_id"] == old_job_id
    assert status["active_reservation"] is False
    assert status["reservation_identity_mismatch"] is True
    assert status["reservation_state"] is None
    assert status["job_lifecycle"] == "process_gone_unknown_outcome"
    assert status["next_poll_after"] == "2026-05-30T13:00:00Z"
    assert reread["last_status_checked_at"] is None


def test_phase5_owner_mutation_refused_for_non_owned_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture, heartbeat=server.reservations.iso_timestamp())

    result = json.loads(server.manage_gpu_job(action="stop", reservation_key="gpu-a.gpu0"))

    assert result["status"] == "refused"
    assert "owns this reservation" in result["reason"]
    assert (registry / "gpu-a.gpu0").exists()


def test_phase5_retry_refuses_while_matching_process_alive(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('holding')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    retry = json.loads(server.manage_gpu_job(action="retry", job_id=launched["job_id"]))

    assert retry["status"] == "refused"
    assert "second process" in retry["reason"]
    assert retry["owned_by_current_server"] is True
    assert retry["allowed_actions"] == ["status", "stop", "retry", "finish"]
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["attempt_id"] == launched["attempt_id"]
    assert metadata["remote_pid"] == 12345


def test_phase5_stop_signals_but_keeps_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "term_delay.py"
    script.write_text("print('term delay')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/term_delay.py",
        output_file=".gpu_mcp_logs/term_delay.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )
    signals = []

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        signals.append(cmd)
        return ""

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    stopped = json.loads(server.manage_gpu_job(action="stop", job_id=launched["job_id"]))

    assert stopped["status"] == "stop_requested"
    assert stopped["job_lifecycle"] == "stopping"
    assert signals == ["kill -15 12345"]
    assert (registry / "gpu-a.gpu0" / "metadata.json").exists()
    assert server.HEARTBEAT_MANAGER.owned_keys() == ["gpu-a.gpu0"]


def test_phase5_local_stop_uses_start_time_when_fingerprint_env_unreadable(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "sleep.py"
    script.write_text("import time\ntime.sleep(30)\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: True)

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/sleep.py",
        output_file=".gpu_mcp_logs/sleep.log",
    ))
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["remote_start_time"]

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if "/proc/" in cmd and "environ" in cmd:
            return None
        if cmd.startswith("ps -p") and "-o pid=" in cmd:
            return f"{metadata['remote_pid']} {server.GPU_MCP_USER} S {metadata['remote_start_time']}"
        return ""

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    stopped = json.loads(server.manage_gpu_job(action="stop", job_id=launched["job_id"]))

    assert stopped["status"] == "stop_requested"
    assert "fingerprint env unreadable but start time matched" in stopped["process"]["reason"]


def test_phase5_retry_after_gone_relaunches_same_job_under_same_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('retry')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", "23456\n"])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": metadata["remote_pid"]},
    )

    retried = json.loads(server.manage_gpu_job(action="retry", job_id=launched["job_id"]))

    assert retried["status"] == "retried"
    assert retried["job_id"] == launched["job_id"]
    assert retried["attempt_id"] != launched["attempt_id"]
    assert retried["reservation_key"] == "gpu-a.gpu0"
    assert retried["process"]["remote_pid"] == 23456
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["job_id"] == launched["job_id"]
    assert metadata["attempt_id"] == retried["attempt_id"]
    assert metadata["remote_pid"] == 23456
    job_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    assert job_record["active_attempt_id"] == retried["attempt_id"]


def test_phase5_retry_local_launch_failure_rolls_back_claim(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('retry fail')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": metadata["remote_pid"]},
    )
    monkeypatch.setattr(server, "_is_local_host", lambda host: True)

    def fail_popen(*args, **kwargs):
        raise OSError("retry spawn failed before process exists")

    monkeypatch.setattr(server.subprocess, "Popen", fail_popen)

    retried = json.loads(server.manage_gpu_job(action="retry", job_id=launched["job_id"]))

    assert retried["status"] == "refused"
    assert "retry spawn failed" in retried["reason"]
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["attempt_id"] == launched["attempt_id"]
    assert metadata["remote_pid"] == 12345
    job_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    assert job_record["active_attempt_id"] == launched["attempt_id"]
    assert job_record["remote_pid"] == 12345


def test_phase5_retry_remote_no_pid_stays_fail_closed(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('retry no pid')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", None])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": metadata["remote_pid"]},
    )

    retried = json.loads(server.manage_gpu_job(action="retry", job_id=launched["job_id"]))

    assert retried["status"] == "launch_outcome_unknown"
    assert "reservation kept fail-closed" in retried["reason"]
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["attempt_id"] == retried["attempt_id"]
    assert metadata["remote_pid"] is None
    job_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    assert job_record["active_attempt_id"] == retried["attempt_id"]
    assert job_record["remote_pid"] is None


def test_pytest_harness_controls_require_explicit_enable(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_probe = fake_bin / "fake-probe"
    fake_probe.write_text("#!/usr/bin/env python3\nprint('fake-ran')\n")
    fake_probe.chmod(0o755)
    control_file = tmp_path / "control.json"
    control_file.write_text(json.dumps({"heartbeat_unhealthy_reason": "disabled controls should not apply"}))
    monkeypatch.setenv("GPU_MCP_TEST_FAKE_BIN", str(fake_bin))
    monkeypatch.setenv("GPU_MCP_TEST_CONTROL_FILE", str(control_file))
    monkeypatch.delenv("GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS", raising=False)
    server = _import_server_with_config(monkeypatch, config)

    assert server._local_shell_run("fake-probe") is None
    server._apply_test_controls_from_file()
    assert server.HEARTBEAT_MANAGER.is_healthy()


def test_pytest_harness_controls_apply_only_when_explicitly_enabled(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_probe = fake_bin / "fake-probe"
    fake_probe.write_text("#!/usr/bin/env python3\nprint('fake-ran')\n")
    fake_probe.chmod(0o755)
    control_file = tmp_path / "control.json"
    control_file.write_text(json.dumps({"heartbeat_unhealthy_reason": "enabled control"}))
    monkeypatch.setenv("GPU_MCP_TEST_FAKE_BIN", str(fake_bin))
    monkeypatch.setenv("GPU_MCP_TEST_CONTROL_FILE", str(control_file))
    monkeypatch.setenv("GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS", "1")
    server = _import_server_with_config(monkeypatch, config)

    assert server._local_shell_run("fake-probe") == "fake-ran"
    server._apply_test_controls_from_file()
    assert not server.HEARTBEAT_MANAGER.is_healthy()
    assert "enabled control" in server.HEARTBEAT_MANAGER.health_reason()


def test_phase5_retry_claim_prevents_stale_second_relaunch(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('retry race')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", "23456\n"])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    old_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    old_metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    first_attempt = server.reservations.generate_attempt_id(suffix="first")
    second_attempt = server.reservations.generate_attempt_id(suffix="second")

    first = json.loads(server._start_managed_attempt(
        job_record=old_record,
        metadata=old_metadata,
        attempt_id=first_attempt,
        output_path=repo_fixture / ".gpu_mcp_logs" / "hold.log",
        next_poll_after="2026-05-30T13:00:00Z",
        async_mode_requested=False,
    ))
    second = json.loads(server._start_managed_attempt(
        job_record=old_record,
        metadata=old_metadata,
        attempt_id=second_attempt,
        output_path=repo_fixture / ".gpu_mcp_logs" / "hold.log",
        next_poll_after="2026-05-30T13:00:00Z",
        async_mode_requested=False,
    ))

    assert first["status"] == "retried"
    assert second["status"] == "refused"
    assert "reservation changed before retry launch" in second["reason"]
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["attempt_id"] == first_attempt
    assert metadata["remote_pid"] == 23456


def test_phase5_retry_missing_pid_with_ack_file_stays_fail_closed(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('retry ack uncertain')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    old_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    old_metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    attempt_id = server.reservations.generate_attempt_id(suffix="ackuncertain")
    ack_path = (
        server.reservations.outcome_record_path(repo_fixture, launched["job_id"], attempt_id).parent
        / "launcher_pid.json"
    )
    ack_path.parent.mkdir(parents=True)
    ack_path.write_text("")
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: None)

    retried = json.loads(server._start_managed_attempt(
        job_record=old_record,
        metadata=old_metadata,
        attempt_id=attempt_id,
        output_path=repo_fixture / ".gpu_mcp_logs" / "hold.log",
        next_poll_after="2026-05-30T13:00:00Z",
        async_mode_requested=False,
    ))

    assert retried["status"] == "launch_outcome_unknown"
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata["attempt_id"] == attempt_id
    assert metadata["remote_pid"] is None
    job_record = json.loads(
        (repo_fixture / ".gpu_mcp_state" / "jobs" / launched["job_id"] / "job.json").read_text()
    )
    assert job_record["active_attempt_id"] == attempt_id
    assert job_record["remote_pid"] is None


def test_phase5_finish_gone_removes_reservation_immediately(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "quick.py"
    script.write_text("print('done')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/quick.py",
        output_file=".gpu_mcp_logs/quick.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": 12345},
    )

    finished = json.loads(server.manage_gpu_job(action="finish", job_id=launched["job_id"]))

    assert finished["status"] == "finished"
    assert finished["job_lifecycle"] == "finished"
    assert not (registry / "gpu-a.gpu0").exists()
    assert server.HEARTBEAT_MANAGER.owned_keys() == []


def test_phase6_status_action_writes_status_acknowledgement(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still alive')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.delenv("GPU_MCP_HOOK_REMINDER_MODE", raising=False)
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    before_status = json.loads(record_path.read_text())
    due_record = dict(before_status)
    due_record["next_poll_after"] = "2000-01-01T00:00:00Z"
    server.reservations.atomic_write_json(record_path, due_record)
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(action="status", job_id=launched["job_id"]))
    after_status = json.loads(record_path.read_text())
    hook = importlib.import_module("gpu_mcp_policy_hook")
    reminder = hook.check_gpu_job_reminders(repo_fixture)

    assert before_status["last_status_checked_at"] is None
    assert status["status"] == "ok"
    assert status["job_lifecycle"] == "running"
    assert after_status["last_status_checked_at"] is not None
    assert after_status["next_poll_after"] == status["next_poll_after"]
    assert reminder is None


def test_phase7_async_launch_defaults_main_and_soft_refuses_without_smoke(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)

    try:
        raw = server.run_python_on_gpu(
            host="gpu-a",
            gpu_index=0,
            script_path="jobs/train.py",
            async_mode=True,
            output_file=".gpu_mcp_logs/train.log",
            expected_duration_sec=3600,
        )
    except TypeError as exc:
        pytest.fail(f"run_python_on_gpu must accept Phase 7 cadence inputs: {exc}")
    result = json.loads(raw)

    assert result["status"] == "refused"
    assert result["job_role"] == "main"
    assert result["job_role_defaulted"] is True
    assert "smoke" in result["reason"]
    assert "smoke_skip_reason" in result["reason"]
    assert result["cadence_basis"]["expected_duration_sec"] == 3600
    assert result["cadence_basis"]["positive_viability_evidence"] is False
    assert not (registry / "gpu-a.gpu0").exists()


def test_phase7_status_before_next_poll_is_compact_and_local_only(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    metadata_before = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    original = json.loads(record_path.read_text())
    original["next_poll_after"] = "2099-01-01T00:00:00Z"
    original["last_status_checked_at"] = None
    server.reservations.atomic_write_json(record_path, original)
    inspections = []
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: inspections.append(metadata)
        or {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(action="status", job_id=launched["job_id"]))
    reread = json.loads(record_path.read_text())

    assert status["status"] == "ok"
    assert status.get("polling_state") == "not_due_yet"
    assert status["full_status_performed"] is False
    assert status["remote_inspection_performed"] is False
    assert status["log_tail_included"] is False
    assert status["job_id"] == launched["job_id"]
    assert status["reservation_key"] == launched["reservation_key"]
    assert status["heartbeat_interval_sec"] == 60
    assert status["next_poll_after"] == "2099-01-01T00:00:00Z"
    assert status["seconds_until_due"] > 0
    assert inspections == []
    assert reread["next_poll_after"] == "2099-01-01T00:00:00Z"
    assert reread["last_status_checked_at"] is None
    assert json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text()) == metadata_before


def test_phase7_expected_duration_selects_short_cadence_for_main_launch(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/train.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/train.log",
        expected_duration_sec=120,
        smoke_skip_reason="test intentionally skips smoke to exercise cadence",
    ))
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert launched["status"] == "launched"
    assert launched["job_role"] == "main"
    assert launched["heartbeat_interval_sec"] == 60
    assert launched["cadence_basis"]["source"] == "expected_duration"
    assert launched["cadence_basis"]["expected_duration_sec"] == 120
    assert metadata["heartbeat_interval_sec"] == 60
    _assert_phase7_private_fields_not_shared(metadata)


def test_phase7_early_poll_reason_forces_full_status_and_records_override(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    record = json.loads(record_path.read_text())
    record["next_poll_after"] = "2099-01-01T00:00:00Z"
    server.reservations.atomic_write_json(record_path, record)
    inspections = []
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: inspections.append(metadata)
        or {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(
        action="status",
        job_id=launched["job_id"],
        early_poll_reason="user explicitly asked for an immediate check",
    ))
    reread = json.loads(record_path.read_text())

    assert status["status"] == "ok"
    assert status["job_lifecycle"] == "running"
    assert status["full_status_performed"] is True
    assert status["remote_inspection_performed"] is True
    assert status["early_poll_override_recorded"] is True
    assert inspections
    assert reread["last_early_poll_reason"] == "user explicitly asked for an immediate check"
    assert reread["last_early_poll_at"] is not None


def test_phase7_update_cadence_clamps_hint_and_updates_heartbeat_metadata(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])

    try:
        updated = json.loads(server.manage_gpu_job(
            action="update_cadence",
            job_id=launched["job_id"],
            cadence_hint_sec=45,
            reason="initial smoke estimate was too aggressive",
        ))
    except TypeError as exc:
        pytest.fail(f"manage_gpu_job must accept Phase 7 cadence update inputs: {exc}")
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    record = json.loads(record_path.read_text())

    assert updated["status"] == "ok"
    assert updated["action"] == "update_cadence"
    assert updated["heartbeat_interval_sec"] == 60
    assert updated["cadence_basis"]["source"] == "cadence_hint"
    assert updated["cadence_basis"]["cadence_hint_sec"] == 45
    assert updated["next_poll_after"] == record["next_poll_after"]
    assert metadata["heartbeat_interval_sec"] == 60
    assert metadata["last_heartbeat_at"] == record["last_heartbeat_at"]
    _assert_phase7_private_fields_not_shared(metadata)
    assert record["cadence_update_reason"] == "initial smoke estimate was too aggressive"


def test_phase7_async_false_defaults_one_off_and_reports_role(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "probe.py"
    script.write_text("print('probe')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/probe.py",
        async_mode=False,
        output_file=".gpu_mcp_logs/probe.log",
    ))

    assert launched["status"] == "launched"
    assert launched["job_role"] == "one_off"
    assert launched["job_role_defaulted"] is True
    assert launched["heartbeat_interval_sec"] == 60
    assert launched["cadence_basis"]["source"] == "conservative_no_evidence"
    assert launched["cadence_basis"]["selected_interval_sec"] == 60


def test_phase7_cadence_hint_clamps_and_wins_over_expected_duration(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/train.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/train.log",
        smoke_skip_reason="user approved skipping smoke for this contract test",
        expected_duration_sec=7201,
        cadence_hint_sec=5000,
    ))
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert launched["status"] == "launched"
    assert launched["heartbeat_interval_sec"] == 3600
    assert launched["cadence_basis"]["source"] == "cadence_hint"
    assert launched["cadence_basis"]["cadence_hint_sec"] == 5000
    assert launched["cadence_basis"]["expected_duration_sec"] == 7201
    assert launched["cadence_basis"]["selected_interval_sec"] == 3600
    assert metadata["heartbeat_interval_sec"] == 3600
    _assert_phase7_private_fields_not_shared(metadata)


def test_phase7_expected_duration_uses_pinned_coarse_bands(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    cases = [
        (300, 60),
        (301, 180),
        (1800, 180),
        (1801, 600),
        (7200, 600),
        (7201, 1800),
    ]

    for gpu_index, (expected_duration, selected_interval) in enumerate(cases):
        launched = json.loads(server.run_python_on_gpu(
            host="gpu-a",
            gpu_index=gpu_index,
            script_path="jobs/train.py",
            async_mode=True,
            output_file=f".gpu_mcp_logs/train-{gpu_index}.log",
            smoke_skip_reason="user approved skipping smoke for this contract test",
            expected_duration_sec=expected_duration,
        ))

        assert launched["status"] == "launched"
        assert launched["heartbeat_interval_sec"] == selected_interval
        assert launched["cadence_basis"]["source"] == "expected_duration"
        assert launched["cadence_basis"]["expected_duration_sec"] == expected_duration
        assert launched["cadence_basis"]["selected_interval_sec"] == selected_interval


def test_phase7_invalid_cadence_inputs_are_refused_without_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    cases = [
        {"cadence_hint_sec": True},
        {"cadence_hint_sec": "60"},
        {"cadence_hint_sec": 0},
        {"cadence_hint_sec": -1},
        {"cadence_hint_sec": float("inf")},
        {"expected_duration_sec": False},
        {"expected_duration_sec": "300"},
        {"expected_duration_sec": 0},
    ]

    for kwargs in cases:
        result = json.loads(server.run_python_on_gpu(
            host="gpu-a",
            gpu_index=0,
            script_path="jobs/train.py",
            async_mode=True,
            output_file=".gpu_mcp_logs/train.log",
            smoke_skip_reason="user approved skipping smoke for this contract test",
            **kwargs,
        ))

        assert result["status"] == "refused"
        assert "cadence" in result["reason"] or "duration" in result["reason"]
    assert not registry.exists() or not any(registry.iterdir())


def test_phase7_main_with_skip_but_no_cadence_uses_conservative_basis(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/train.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/train.log",
        smoke_skip_reason="user says this short run is itself the smoke check",
    ))
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert launched["status"] == "launched"
    assert launched["job_role"] == "main"
    assert launched["heartbeat_interval_sec"] == 60
    assert launched["cadence_basis"]["source"] == "conservative_no_evidence"
    assert launched["cadence_basis"]["conservative_reason"] == "smoke_skipped"
    assert launched["cadence_basis"]["skip_reason_recorded"] is True
    assert launched["cadence_basis"]["selected_interval_sec"] == 60
    assert metadata["heartbeat_interval_sec"] == 60
    _assert_phase7_private_fields_not_shared(metadata)


def test_phase7_smoke_launch_is_managed_and_uses_minimum_first_check_without_hint(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "smoke.py"
    script.write_text("print('smoke')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/smoke.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/smoke.log",
        job_role="smoke",
    ))
    record = json.loads(server.reservations.job_record_path(repo_fixture, launched["job_id"]).read_text())
    metadata = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())

    assert launched["status"] == "launched"
    assert launched["job_role"] == "smoke"
    assert launched["job_role_defaulted"] is False
    assert launched["heartbeat_interval_sec"] == 60
    assert launched["cadence_basis"]["source"] == "conservative_no_evidence"
    assert launched["cadence_basis"]["conservative_reason"] == "missing_cadence_evidence"
    assert record["job_role"] == "smoke"
    assert metadata["heartbeat_interval_sec"] == 60
    _assert_phase7_private_fields_not_shared(metadata)


def test_phase7_successful_smoke_job_is_viability_only_without_cadence_signal(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    smoke = _seed_phase7_smoke_record(server, repo_fixture, runtime_sec=1801)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/train.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/train.log",
        smoke_job_id=smoke["job_id"],
    ))

    assert launched["status"] == "launched"
    assert launched["heartbeat_interval_sec"] == 60
    assert launched["cadence_basis"]["source"] == "smoke_job"
    assert launched["cadence_basis"]["smoke_job_id"] == smoke["job_id"]
    assert launched["cadence_basis"]["smoke_lifecycle"] == "succeeded"
    assert launched["cadence_basis"]["positive_viability_evidence"] is True
    assert launched["cadence_basis"]["cadence_evidence_used"] is False
    assert launched["cadence_basis"]["conservative_reason"] == "smoke_observation_only"
    assert launched["cadence_basis"]["selected_interval_sec"] == 60


def test_phase7_representative_smoke_runtime_can_drive_cadence_band(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    smoke = _seed_phase7_smoke_record(server, repo_fixture, runtime_sec=1801)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")

    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/train.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/train.log",
        smoke_job_id=smoke["job_id"],
        smoke_cadence_representative=True,
    ))

    assert launched["status"] == "launched"
    assert launched["heartbeat_interval_sec"] == 600
    assert launched["cadence_basis"]["source"] == "smoke_job"
    assert launched["cadence_basis"]["smoke_job_id"] == smoke["job_id"]
    assert launched["cadence_basis"]["smoke_runtime_sec"] == 1801
    assert launched["cadence_basis"]["smoke_cadence_representative"] is True
    assert launched["cadence_basis"]["cadence_evidence_used"] is True
    assert launched["cadence_basis"]["selected_interval_sec"] == 600


def test_phase7_main_launch_refuses_unusable_smoke_references(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "train.py"
    script.write_text("print('training')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    failed_smoke = _seed_phase7_smoke_record(server, repo_fixture, terminal_status="failure")
    wrong_role = _seed_phase7_smoke_record(server, repo_fixture, job_role="main")
    cases = [
        ("job-20260530T120000Z-missing", None),
        (failed_smoke["job_id"], "failed"),
        (wrong_role["job_id"], None),
    ]

    for smoke_job_id, smoke_lifecycle in cases:
        result = json.loads(server.run_python_on_gpu(
            host="gpu-a",
            gpu_index=0,
            script_path="jobs/train.py",
            async_mode=True,
            output_file=".gpu_mcp_logs/train.log",
            smoke_job_id=smoke_job_id,
        ))

        assert result["status"] == "refused"
        assert result["cadence_basis"]["smoke_job_id"] == smoke_job_id
        assert result["cadence_basis"]["positive_viability_evidence"] is False
        if smoke_lifecycle is not None:
            assert result["cadence_basis"]["smoke_lifecycle"] == smoke_lifecycle
    assert not registry.exists() or not any(registry.iterdir())


@pytest.mark.parametrize(
    ("stored_value", "remove_field"),
    [
        (None, True),
        (None, False),
        ("", False),
        ("not-a-date", False),
        ("2099-01-01T00:00:00+00:00", False),
        ("2099-01-01T00:00:00Z garbage", False),
    ],
)
def test_phase7_malformed_next_poll_after_takes_full_status_path(
    repo_fixture,
    tmp_path,
    monkeypatch,
    stored_value,
    remove_field,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    record = json.loads(record_path.read_text())
    if remove_field:
        record.pop("next_poll_after", None)
    else:
        record["next_poll_after"] = stored_value
    server.reservations.atomic_write_json(record_path, record)
    inspections = []
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: inspections.append(metadata)
        or {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    status = json.loads(server.manage_gpu_job(action="status", job_id=launched["job_id"]))
    reread = json.loads(record_path.read_text())

    assert status["status"] == "ok"
    assert status.get("polling_state") != "not_due_yet"
    assert status["full_status_performed"] is True
    assert status["remote_inspection_performed"] is True
    assert inspections
    assert reread["last_status_checked_at"] is not None


def test_phase7_local_terminal_outcome_before_due_returns_full_terminal_status(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "quick.py"
    script.write_text("print('done')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/quick.py",
        output_file=".gpu_mcp_logs/quick.log",
    )
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    record = json.loads(record_path.read_text())
    record["next_poll_after"] = "2099-01-01T00:00:00Z"
    server.reservations.atomic_write_json(record_path, record)
    attempt_id = record["active_attempt_id"]
    server.reservations.atomic_write_json(
        server.reservations.outcome_record_path(repo_fixture, launched["job_id"], attempt_id),
        server.reservations.build_outcome_record(
            job_id=launched["job_id"],
            attempt_id=attempt_id,
            reservation_key_value=launched["reservation_key"],
            host="gpu-a",
            gpu_index=0,
            terminal_status="success",
            started_at="2026-05-30T12:00:00Z",
            ended_at="2026-05-30T12:00:05Z",
            remote_pid=record["remote_pid"],
            exit_code=0,
        ),
    )
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process exited", "remote_pid": record["remote_pid"]},
    )

    status = json.loads(server.manage_gpu_job(action="status", job_id=launched["job_id"]))

    assert status["status"] == "ok"
    assert status.get("polling_state") != "not_due_yet"
    assert status["full_status_performed"] is True
    assert status["job_lifecycle"] == "succeeded"
    assert status["outcome"]["terminal_status"] == "success"
    assert status["next_poll_after"] is None


def test_phase7_multi_job_status_gating_is_per_job(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    future = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/future.log",
    )
    due = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=1,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/due.log",
    )
    future_record_path = server.reservations.job_record_path(repo_fixture, future["job_id"])
    due_record_path = server.reservations.job_record_path(repo_fixture, due["job_id"])
    future_record = json.loads(future_record_path.read_text())
    future_record["next_poll_after"] = "2099-01-01T00:00:00Z"
    due_record = json.loads(due_record_path.read_text())
    due_record["next_poll_after"] = "2000-01-01T00:00:00Z"
    server.reservations.atomic_write_json(future_record_path, future_record)
    server.reservations.atomic_write_json(due_record_path, due_record)
    inspected_keys = []
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: inspected_keys.append(metadata["reservation_key"])
        or {"status": "alive", "reason": "still running", "remote_pid": metadata["remote_pid"]},
    )

    future_status = json.loads(server.manage_gpu_job(action="status", job_id=future["job_id"]))
    due_status = json.loads(server.manage_gpu_job(action="status", job_id=due["job_id"]))

    assert future_status["polling_state"] == "not_due_yet"
    assert future_status["remote_inspection_performed"] is False
    assert due_status.get("polling_state") != "not_due_yet"
    assert due_status["full_status_performed"] is True
    assert due_status["remote_inspection_performed"] is True
    assert inspected_keys == [due["reservation_key"]]


def test_phase7_update_cadence_refuses_missing_reason_or_cadence_input(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    metadata_before = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    cases = [
        {"cadence_hint_sec": 300},
        {"cadence_hint_sec": 300, "reason": ""},
        {"cadence_hint_sec": 300, "reason": "   "},
        {"reason": "need a different cadence"},
    ]

    for kwargs in cases:
        result = json.loads(server.manage_gpu_job(
            action="update_cadence",
            job_id=launched["job_id"],
            **kwargs,
        ))

        assert result["status"] == "refused"
        assert result["action"] == "update_cadence"
    metadata_after = json.loads((registry / "gpu-a.gpu0" / "metadata.json").read_text())
    assert metadata_after["heartbeat_interval_sec"] == metadata_before["heartbeat_interval_sec"]
    assert metadata_after["last_heartbeat_at"] == metadata_before["last_heartbeat_at"]


def test_phase7_update_cadence_refuses_non_owner_without_mutating_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still running')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = _launch_phase7_setup_job(
        server,
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    )
    record_path = server.reservations.job_record_path(repo_fixture, launched["job_id"])
    record_before = json.loads(record_path.read_text())
    metadata_path = registry / "gpu-a.gpu0" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["server_instance_id"] = "server-gpu-a-9999-other"
    server.reservations.atomic_write_json(metadata_path, metadata)

    result = json.loads(server.manage_gpu_job(
        action="update_cadence",
        job_id=launched["job_id"],
        cadence_hint_sec=300,
        reason="agent learned the run will take longer",
    ))
    reread = json.loads(metadata_path.read_text())
    record_after = json.loads(record_path.read_text())

    assert result["status"] == "refused"
    assert "owner" in result["reason"] or "owned" in result["reason"]
    assert reread["server_instance_id"] == "server-gpu-a-9999-other"
    assert reread["heartbeat_interval_sec"] == metadata["heartbeat_interval_sec"]
    assert reread["last_heartbeat_at"] == metadata["last_heartbeat_at"]
    assert record_after.get("next_poll_after") == record_before.get("next_poll_after")
    assert record_after.get("last_heartbeat_at") == record_before.get("last_heartbeat_at")
    assert record_after.get("cadence_update_reason") == record_before.get("cadence_update_reason")


def test_phase5_finish_alive_refuses_and_keeps_heartbeat(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('still alive')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "12345\n")
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "alive", "reason": "still running", "remote_pid": 12345},
    )

    finished = json.loads(server.manage_gpu_job(action="finish", job_id=launched["job_id"]))
    listed = json.loads(server.list_gpu_reservations(scope="mine", fresh=False))

    assert finished["status"] == "refused"
    assert "stop" in finished["reason"]
    assert "status" in finished["reason"]
    assert "finish" in finished["reason"]
    assert (registry / "gpu-a.gpu0" / "metadata.json").exists()
    assert server.HEARTBEAT_MANAGER.owned_keys() == ["gpu-a.gpu0"]
    assert listed["reservations"][0]["owned_by_current_server"] is True
    assert listed["reservations"][0]["allowed_actions"] == ["status", "stop", "retry", "finish"]


def test_phase5_zombie_is_process_gone_for_retry(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "hold.py"
    script.write_text("print('zombie retry')\n")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    pids = iter(["12345\n", "23456\n"])
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: next(pids))
    launched = json.loads(server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/hold.py",
        output_file=".gpu_mcp_logs/hold.log",
    ))
    monkeypatch.setattr(
        server,
        "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "process is zombie", "remote_pid": 12345},
    )

    retried = json.loads(server.manage_gpu_job(action="retry", job_id=launched["job_id"]))

    assert retried["status"] == "retried"
    assert retried["process"]["remote_pid"] == 23456


def test_phase5_kill_gpu_process_does_not_remove_reservation(
    repo_fixture,
    tmp_path,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    server = _import_server_with_config(monkeypatch, config)
    _seed_reservation(registry, repo_fixture, heartbeat=server.reservations.iso_timestamp())
    process_info = {
        "pid": "12345",
        "ppid": "1",
        "pgid": "12345",
        "owner": server.GPU_MCP_USER,
        "start_time": "Thu May 30 12:00:00 2026",
        "command": "python ignore_term.py",
        "cmd_hash": "abc123",
        "cmd_preview": "python ignore_term.py",
        "gpu_index": "0",
        "gpu_memory_mib": "1",
    }
    process_info["fingerprint"] = server._kill_fingerprint("gpu-a", process_info)
    monkeypatch.setattr(server, "_inspect_kill_target", lambda host, pid: process_info)
    monkeypatch.setattr(server, "_host_run", lambda host, cmd, user=server.GPU_MCP_USER, timeout=15: "")

    inspected = json.loads(server.kill_gpu_process(host="gpu-a", pid=12345))
    signaled = json.loads(server.kill_gpu_process(
        host="gpu-a",
        pid=12345,
        fingerprint=inspected["fingerprint"],
        signal="TERM",
    ))

    assert inspected["status"] == "inspect"
    assert signaled["status"] == "signaled"
    assert (registry / "gpu-a.gpu0" / "metadata.json").exists()


def test_phase5_kill_gpu_process_refuses_stale_policy(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_stale_policy_refusal", lambda: "GPU MCP policy changed after server start")

    result = server.kill_gpu_process(host="gpu-a", pid=12345)

    assert "policy changed" in result


def test_nvidia_smi_parser_tolerates_non_numeric_fields(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    parsed = server._parse_nvsmi_csv(
        "0, NVIDIA RTX, N/A, [Not Supported], N/A\n"
        "1, NVIDIA RTX, 12 %, 2048 MiB, 24576 MiB\n"
    )

    assert parsed[0]["utilization_pct"] is None
    assert parsed[0]["memory_used_MiB"] is None
    assert parsed[0]["memory_total_MiB"] is None
    assert parsed[1]["utilization_pct"] == 12


def test_check_gpu_processes_tolerates_malformed_gpu_uuid_rows(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if "query-compute-apps" in cmd:
            return "1234, GPU-deadbeef, 42, python"
        if "query-gpu=index,uuid" in cmd:
            return "N/A, GPU-deadbeef"
        if cmd.startswith("ps -p 1234"):
            return f"{server.GPU_MCP_USER} python train.py"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = server.check_gpu_processes(hosts=["gpu-a"], user_filter=None)

    assert "PID 1234" in result
    assert "GPU ?" in result


def test_check_gpu_processes_builds_gpu_map_per_host(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    server = _import_server_with_config(monkeypatch, config)

    def fake_host_run(host, cmd, user=server.GPU_MCP_USER, timeout=15):
        if "query-compute-apps" in cmd:
            return f"{host[-1]}234, GPU-{host[-1]}, 42, python"
        if "query-gpu=index,uuid" in cmd:
            return ("0, GPU-a" if host == "gpu-a" else "1, GPU-b")
        if cmd.startswith("ps -p"):
            return f"{server.GPU_MCP_USER} python train.py"
        raise AssertionError(cmd)

    monkeypatch.setattr(server, "_host_run", fake_host_run)

    result = server.check_gpu_processes(hosts=["gpu-a", "gpu-b"], user_filter=None)

    assert "[gpu-a]" in result
    assert "GPU 0 | PID a234" in result
    assert "[gpu-b]" in result
    assert "GPU 1 | PID b234" in result


def test_remote_host_run_propagates_timeout(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    seen = {}

    monkeypatch.setattr(server, "_is_local_host", lambda host: False)

    def fake_ssh_run(host, cmd, user=server.GPU_MCP_USER, hide=True, timeout=15):
        seen["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(server, "_ssh_run", fake_ssh_run)

    assert server._host_run("gpu-a", "true", timeout=77) == "ok"
    assert seen["timeout"] == 77


def test_remote_host_run_records_failure_reason(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    monkeypatch.setattr(server, "_is_local_host", lambda host: False)

    class FakeConnection:
        def run(self, cmd, hide=True, timeout=15):
            raise TimeoutError("timed out while connecting")

    monkeypatch.setattr(server, "_conn", lambda host, user=server.GPU_MCP_USER: FakeConnection())

    assert server._host_run("gpu-a", "nvidia-smi", timeout=3) is None
    assert "timeout" in server._host_run_error("gpu-a", "nvidia-smi")


def test_remote_python_argv_uses_staged_safe_runner_not_server(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    server = _import_server_with_config(monkeypatch, config)

    argv = server._build_python_gpu_argv(
        "jobs/ok.py",
        args=["x"],
        execution_host="gpu-a",
    )

    assert str(SERVER) not in argv
    assert "--safe-run" not in argv
    assert str(repo_fixture / ".gpu_mcp_runner" / "gpu_mcp_safe_runner.py") in argv
    assert "--job" in argv
    assert str(script) in argv


def test_build_python_argv_chooses_local_safe_run_and_remote_staged_runner(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    server = _import_server_with_config(monkeypatch, config)

    local_argv = server._build_python_gpu_argv("jobs/ok.py", execution_host="localhost")
    remote_argv = server._build_python_gpu_argv("jobs/ok.py", execution_host="gpu-a")

    assert "--safe-run" in local_argv
    assert str(SERVER) in local_argv
    assert "--safe-run" not in remote_argv
    assert str(repo_fixture / ".gpu_mcp_runner" / "gpu_mcp_safe_runner.py") in remote_argv


def test_staged_safe_runner_replaces_symlink_without_following(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    outside = repo_fixture.parent / "outside_target.py"
    outside.write_text("outside stays intact\n")
    runner_dir = repo_fixture / ".gpu_mcp_runner"
    runner_dir.mkdir()
    runner = runner_dir / "gpu_mcp_safe_runner.py"
    runner.symlink_to(outside)

    staged = server._ensure_staged_safe_runner()

    assert staged == runner
    assert not runner.is_symlink()
    assert "outside stays intact\n" == outside.read_text()
    assert server._safe_runner_source_hash() == server._file_sha256(runner)


def test_staged_runner_replaced_when_hash_mismatched(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    runner_dir = repo_fixture / ".gpu_mcp_runner"
    runner_dir.mkdir()
    runner = runner_dir / "gpu_mcp_safe_runner.py"
    runner.write_text("# old runner\n")
    old_hash = server._file_sha256(runner)

    staged = server._ensure_staged_safe_runner()

    assert staged == runner
    assert server._file_sha256(staged) != old_hash
    assert server._safe_runner_source_hash() == server._file_sha256(staged)


def test_staged_runner_bundle_includes_shared_guard_and_manifest_hashes(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    staged = server._ensure_staged_safe_runner()
    runner_dir = staged.parent
    guard = runner_dir / "gpu_mcp_guard.py"
    manifest = json.loads((runner_dir / "runner_manifest.json").read_text())

    assert guard.exists()
    assert manifest["files"]["gpu_mcp_safe_runner.py"]["sha256"] == server._file_sha256(staged)
    assert manifest["files"]["gpu_mcp_guard.py"]["sha256"] == server._file_sha256(guard)


def test_standalone_safe_runner_blocks_write_escape(repo_fixture):
    config = _write_config(repo_fixture)
    outside = repo_fixture.parent / "outside.txt"
    script = repo_fixture / "jobs" / "bad_write.py"
    script.write_text(f"from pathlib import Path\nPath({str(outside)!r}).write_text('bad')\n")
    import gpu_mcp_safe_runner

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(gpu_mcp_safe_runner.__file__).resolve()),
            "--job",
            str(script),
            "--repo-root",
            str(repo_fixture),
            "--script-roots",
            json.dumps([str(repo_fixture / "jobs")]),
            "--write-roots",
            json.dumps([str(repo_fixture / "results")]),
            "--",
        ],
        cwd=repo_fixture,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    assert "blocked write outside approved roots" in completed.stdout
    assert not outside.exists()


def test_staged_runner_bundle_is_pure_stdlib():
    def imported_roots(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(), filename=str(path))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        return roots

    stdlib = set(sys.stdlib_module_names) | {"__future__"}

    assert imported_roots(GUARD) <= stdlib
    assert imported_roots(SAFE_RUNNER) <= stdlib | {"gpu_mcp_guard"}


def test_safe_run_blocks_dup2_fd_redirection(repo_fixture):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "dup2_escape.py"
    script.write_text("import os\nos.dup2(1, 2)\nprint('dup2 ran')\n")

    completed = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--safe-run", str(script)],
        cwd=repo_fixture,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    assert "dup2" in completed.stdout


def test_static_scan_rejects_raw_io_modules(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    io_script = repo_fixture / "jobs" / "io_bypass.py"
    io_script.write_text("import _io\nprint(_io.open)\n")
    posix_script = repo_fixture / "jobs" / "posix_bypass.py"
    posix_script.write_text("import posix\nprint(posix.open)\n")
    server = _import_server_with_config(monkeypatch, config)

    assert any("_io" in issue for issue in server.scan_python_gpu_script_safety(str(io_script)))
    assert any("posix" in issue for issue in server.scan_python_gpu_script_safety(str(posix_script)))


def test_static_scan_rejects_codex_self_spawn_commands(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "codex_bypass.py"
    script.write_text(
        "import os\n"
        "os.system('codex exec --ignore-rules --dangerously-bypass-approvals-and-sandbox run')\n"
    )
    server = _import_server_with_config(monkeypatch, config)

    issues = server.scan_python_gpu_script_safety(str(script))

    assert any("codex" in issue for issue in issues)


def test_guard_run_job_does_not_prepend_script_dir_to_sys_path(repo_fixture):
    (repo_fixture / "jobs" / "os.py").write_text("SHADOWED = True\n")
    script = repo_fixture / "jobs" / "import_os.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path('results/os_origin.txt').write_text(str(os.__dict__.get('SHADOWED', False)))\n"
    )
    code = (
        "from pathlib import Path\n"
        "import sys\n"
        "import gpu_mcp_guard\n"
        "repo = Path(sys.argv[1])\n"
        "script = Path(sys.argv[2])\n"
        "raise SystemExit(gpu_mcp_guard.run_job("
        "str(script), [], repo_root=repo, script_roots=[repo / 'jobs'], write_roots=[repo / 'results']))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code, str(repo_fixture), str(script)],
        cwd=repo_fixture,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert (repo_fixture / "results" / "os_origin.txt").read_text() == "False"


def test_safe_run_delegates_to_shared_guard_sys_path_behavior(repo_fixture):
    config = _write_config(repo_fixture)
    (repo_fixture / "jobs" / "os.py").write_text("SHADOWED = True\n")
    script = repo_fixture / "jobs" / "import_os.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path('results/os_origin_safe_run.txt').write_text(str(os.__dict__.get('SHADOWED', False)))\n"
    )

    completed = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--safe-run", str(script)],
        cwd=repo_fixture,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    assert (repo_fixture / "results" / "os_origin_safe_run.txt").read_text() == "False"


def test_script_symlink_is_rejected_even_when_target_is_inside_repo(repo_fixture):
    config = _write_config(repo_fixture)
    target = repo_fixture / "jobs" / "real.py"
    target.write_text("print('real')\n")
    link = repo_fixture / "jobs" / "link.py"
    link.symlink_to(target)

    completed = subprocess.run(
        [sys.executable, str(SERVER), "--config", str(config), "--safe-run", str(link)],
        cwd=repo_fixture,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode != 0
    assert "symlink" in completed.stdout


def test_script_reader_refuses_final_symlink_swap(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    target = repo_fixture / "jobs" / "real.py"
    target.write_text("print('real')\n")
    link = repo_fixture / "jobs" / "swapped.py"
    link.symlink_to(target)
    server = _import_server_with_config(monkeypatch, config)

    with pytest.raises(OSError):
        server._read_script_no_follow(link)


def test_output_open_rejects_existing_symlink(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    outside = repo_fixture.parent / "outside.log"
    link = repo_fixture / ".gpu_mcp_logs" / "link.log"
    link.symlink_to(outside)
    server = _import_server_with_config(monkeypatch, config)

    with pytest.raises(OSError):
        server._open_output_no_follow(link)

    assert not outside.exists()


def test_output_parent_must_not_be_symlink(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    outside = repo_fixture.parent / "outside_logs"
    outside.mkdir()
    logs = repo_fixture / ".gpu_mcp_logs"
    logs.rmdir()
    logs.symlink_to(outside)

    with pytest.raises(ValueError, match="parent must not be a symlink"):
        server._prepare_output_parent(logs / "job.log")


def test_async_remote_launch_rejects_invalid_pid(repo_fixture, tmp_path, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "reservations"))
    server = _import_server_with_config(monkeypatch, config)
    monkeypatch.setattr(server, "_is_local_host", lambda host: False)
    monkeypatch.setattr(server, "_ssh_run", lambda host, cmd: "not-a-pid\n")

    result = server.run_python_on_gpu(
        host="gpu-a",
        gpu_index=0,
        script_path="jobs/ok.py",
        async_mode=True,
        output_file=".gpu_mcp_logs/job.log",
    )

    parsed = json.loads(result)
    assert parsed["status"] == "launch_outcome_unknown"
    assert "invalid async pid" in parsed["reason"]
    assert parsed["job_id"].startswith("job-")
    assert parsed["reservation_key"] == "gpu-a.gpu0"
    assert (tmp_path / "reservations" / "gpu-a.gpu0" / "metadata.json").exists()


def test_kill_rejects_bool_pid(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    result = server.kill_gpu_process(host="gpu-a", pid=True)

    assert '"status": "refused"' in result
    assert "pid must be an integer" in result


def test_local_shell_run_preserves_stderr_on_success(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    result = server._local_shell_run(
        shlex.join([sys.executable, "-c", "import sys; print('out'); print('warn', file=sys.stderr)"])
    )

    assert "out" in result
    assert "warn" in result


def test_conn_rejects_symlink_ssh_key(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)
    target = repo_fixture / "real_key"
    target.write_text("not really a key")
    key_link = repo_fixture / "gpu_mcp_key"
    key_link.symlink_to(target)

    monkeypatch.setattr(server, "GPU_MCP_SSH_KEY", str(key_link))

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        server._conn("gpu-a")


def test_local_host_alias_does_not_assume_mit_domain(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    server = _import_server_with_config(monkeypatch, config)

    monkeypatch.setattr(server.os, "uname", lambda: type("Uname", (), {"nodename": "gpu01"})())

    assert server._is_local_host("gpu01")
    assert server._is_local_host("gpu01.example.edu")


def test_canonical_policy_host_prefers_exact_fqdn_and_rejects_ambiguous_short(
    repo_fixture,
    monkeypatch,
):
    config = _write_config(repo_fixture)
    config.write_text(
        config.read_text().replace(
            "nodes = ['gpu-a']",
            "nodes = ['gpu-a.domain1.example', 'gpu-a.domain2.example']",
        )
    )
    server = _import_server_with_config(monkeypatch, config)

    assert server._canonical_policy_host("gpu-a.domain2.example") == "gpu-a.domain2.example"
    with pytest.raises(ValueError, match="ambiguous"):
        server._canonical_policy_host("gpu-a")
