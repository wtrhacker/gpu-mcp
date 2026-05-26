from __future__ import annotations

"""Contract tests for real server startup and safe-run boundaries."""

import os
import ast
import importlib
import json
import shlex
import subprocess
import sys
import uuid
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
    sys.modules.pop("gpu_mcp_server", None)
    return importlib.import_module("gpu_mcp_server")


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

    assert "gpu_index" in result
    assert "integer" in result


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

    assert "args" in args_result
    assert "async_mode" in async_result


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


def test_async_remote_launch_rejects_invalid_pid(repo_fixture, monkeypatch):
    config = _write_config(repo_fixture)
    script = repo_fixture / "jobs" / "ok.py"
    script.write_text("print('ok')\n")
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

    assert "invalid async pid" in result


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
