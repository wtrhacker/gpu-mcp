from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy
import gpu_mcp_reservations as reservations


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos" / "repo_local_codex"
SERVER = REPO_ROOT / "test" / "support" / "proxy_for_codex_exec.py"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _write_repo(name: str, host: str, marker: str, *, fixture_root: Path = FIXTURE_ROOT) -> Path:
    repo = fixture_root / name
    jobs = repo / "jobs"
    results = repo / "results"
    codex_dir = repo / ".codex"
    jobs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    codex_dir.mkdir(parents=True, exist_ok=True)

    (jobs / "ok_job.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "",
                "root = Path(__file__).resolve().parents[1]",
                f"(root / 'results' / 'marker.txt').write_text({marker!r})",
                f"print({marker!r})",
                "",
            ]
        )
    )
    (repo / "gpu-mcp.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                f"nodes = [{host!r}]",
                "script_roots = ['jobs']",
                "write_roots = ['results']",
                "output_roots = ['results']",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                "sync_timeout_sec = 5",
                "",
            ]
        )
    )
    approve_policy(
        load_policy(repo / "gpu-mcp.toml"),
        store_path=repo / ".gpu_mcp_state" / "approved-policies.json",
        diff_summary=["repo-local codex fixture approval"],
        approved_by="pytest",
    )
    (codex_dir / "config.toml").write_text(
        "\n".join(
            [
                "[features]",
                "hooks = true",
                "",
                f"[mcp_servers.{name}-gpu-probe]",
                f"command = {sys.executable!r}",
                "args = [",
                f"  {str(SERVER)!r},",
                '  "serve",',
                '  "--config",',
                f"  {str(repo / 'gpu-mcp.toml')!r},",
                "]",
                "enabled = true",
                "startup_timeout_sec = 20",
                "tool_timeout_sec = 10",
                "",
                f"[mcp_servers.{name}-gpu-probe.tools.run_python_on_gpu]",
                'approval_mode = "approve"',
                "",
            ]
        )
    )
    return repo


@pytest.fixture()
def repo_local_fixture(tmp_path):
    fixture_root = tmp_path / "repo_local_codex"
    repo_a = _write_repo("repo-a", "gpu-a", "repo-a-ran", fixture_root=fixture_root)
    repo_b = _write_repo("repo-b", "gpu-b", "repo-b-ran", fixture_root=fixture_root)
    return repo_a, repo_b


def _server_config(repo: Path) -> dict:
    config_path = repo / ".codex" / "config.toml"
    return _load_toml(config_path)


def test_repo_local_codex_configs_point_to_matching_gpu_mcp_policy(repo_local_fixture):
    repo_a, repo_b = repo_local_fixture
    config_a = _server_config(repo_a)
    config_b = _server_config(repo_b)

    server_a = config_a["mcp_servers"]["repo-a-gpu-probe"]
    server_b = config_b["mcp_servers"]["repo-b-gpu-probe"]

    assert server_a["command"] == sys.executable
    assert server_a["args"] == [
        str(SERVER),
        "serve",
        "--config",
        str(repo_a / "gpu-mcp.toml"),
    ]
    assert server_a["tools"]["run_python_on_gpu"]["approval_mode"] == "approve"

    assert server_b["command"] == sys.executable
    assert server_b["args"] == [
        str(SERVER),
        "serve",
        "--config",
        str(repo_b / "gpu-mcp.toml"),
    ]
    assert server_b["tools"]["run_python_on_gpu"]["approval_mode"] == "approve"


def test_repo_local_gpu_mcp_policies_are_distinct(repo_local_fixture):
    repo_a, repo_b = repo_local_fixture

    policy_a = _load_toml(repo_a / "gpu-mcp.toml")
    policy_b = _load_toml(repo_b / "gpu-mcp.toml")

    assert policy_a["repo_root"] == str(repo_a)
    assert policy_b["repo_root"] == str(repo_b)
    assert policy_a["nodes"] == ["gpu-a"]
    assert policy_b["nodes"] == ["gpu-b"]


def _run_codex_exec(repo: Path, prompt: str, output_name: str, *, env: dict[str, str] | None = None) -> str:
    output_path = repo / output_name
    child_env = os.environ.copy()
    child_env.setdefault("PYTEST_CURRENT_TEST", os.environ.get("PYTEST_CURRENT_TEST", "codex-repo-local-mcp"))
    if env:
        child_env.update(env)
    policy_store = repo / ".gpu_mcp_state" / "approved-policies.json"
    if policy_store.exists():
        child_env.setdefault("GPU_MCP_TEST_POLICY_APPROVAL_STORE", str(policy_store))
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "-C",
            str(repo),
            "-c",
            f"projects.{json.dumps(str(repo))}.trust_level=\"trusted\"",
            "--enable",
            "hooks",
            "--sandbox",
            "workspace-write",
            "--dangerously-bypass-hook-trust",
            "--output-last-message",
            str(output_path),
            prompt,
        ],
        cwd=REPO_ROOT,
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    return output_path.read_text()


def _extract_mcp_result(final_message: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(final_message):
        if char != "{":
            continue
        try:
            envelope, _ = decoder.raw_decode(final_message[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict) and "result" in envelope:
            return json.loads(envelope["result"])
    raise AssertionError(f"no MCP result envelope found in final message:\n{final_message}")


live_codex_exec = pytest.mark.skipif(
    os.environ.get("GPU_MCP_RUN_CODEX_EXEC_TESTS") != "1",
    reason="set GPU_MCP_RUN_CODEX_EXEC_TESTS=1 to run live codex exec probes",
)


@pytest.mark.codex_exec
@live_codex_exec
def test_codex_exec_loads_repo_a_local_mcp_config(repo_local_fixture):
    repo_a, _ = repo_local_fixture

    result = _run_codex_exec(
        repo=repo_a,
        output_name="codex_exec_pytest_repo_a.txt",
        prompt=(
            "Do not run shell commands. Do not edit files. Use the MCP tool "
            "repo-a-gpu-probe/run_python_on_gpu with host='gpu-a' and "
            "script_path='jobs/ok_job.py'. Then report the exact tool result."
        ),
    )

    parsed = _extract_mcp_result(result)
    assert parsed["status"] == "ok"
    assert parsed["repo_root"] == str(repo_a)
    assert parsed["stdout"] == "repo-a-ran\n"


@pytest.mark.codex_exec
@live_codex_exec
def test_codex_exec_loads_repo_b_local_mcp_config(repo_local_fixture):
    _, repo_b = repo_local_fixture

    result = _run_codex_exec(
        repo=repo_b,
        output_name="codex_exec_pytest_repo_b.txt",
        prompt=(
            "Do not run shell commands. Do not edit files. Use the MCP tool "
            "repo-b-gpu-probe/run_python_on_gpu with host='gpu-b' and "
            "script_path='jobs/ok_job.py'. Then report the exact tool result."
        ),
    )

    parsed = _extract_mcp_result(result)
    assert parsed["status"] == "ok"
    assert parsed["repo_root"] == str(repo_b)
    assert parsed["stdout"] == "repo-b-ran\n"


@pytest.mark.codex_exec
@live_codex_exec
def test_codex_exec_repo_a_rejects_repo_b_host(repo_local_fixture):
    repo_a, _ = repo_local_fixture

    result = _run_codex_exec(
        repo=repo_a,
        output_name="codex_exec_pytest_repo_a_rejects_gpu_b.txt",
        prompt=(
            "Do not run shell commands. Do not edit files. Use the MCP tool "
            "repo-a-gpu-probe/run_python_on_gpu with host='gpu-b' and "
            "script_path='jobs/ok_job.py'. Report the exact tool result."
        ),
    )

    parsed = _extract_mcp_result(result)
    assert parsed["status"] == "rejected"
    assert parsed["error"] == "host not allowed: gpu-b"


@pytest.mark.codex_exec
@live_codex_exec
def test_codex_exec_installed_pretooluse_reminder_context_is_model_visible(tmp_path):
    repo = REPO_ROOT
    installed_hook = Path("/home/tingran/gpu-mcp/gpu_mcp_policy_hook.py")
    assert installed_hook.read_bytes() == (REPO_ROOT / "gpu_mcp_policy_hook.py").read_bytes()
    registry = tmp_path / "reservations"
    suffix = f"phase6probe_{secrets.token_hex(4)}"
    job_id = reservations.generate_job_id(
        now=datetime(2026, 5, 30, 12, 10, tzinfo=timezone.utc),
        suffix=suffix,
    )
    attempt_id = reservations.generate_attempt_id(
        now=datetime(2026, 5, 30, 12, 10, tzinfo=timezone.utc),
        suffix=suffix,
    )
    server_id = reservations.generate_server_instance_id(
        hostname="phase6-probe",
        pid=os.getpid(),
        suffix=suffix,
    )
    reservation_key = reservations.reservation_key("localhost", 7)
    reservation_dir = registry / reservation_key
    reminder_path = reservations.hook_reminder_path(repo, job_id)
    job_record_path = reservations.job_record_path(repo, job_id)

    try:
        reservation_dir.mkdir(parents=True)
        reservations.atomic_write_json(
            reservation_dir / "metadata.json",
            reservations.build_shared_metadata(
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key,
                host="localhost",
                gpu_index=7,
                repo=repo,
                script_path=repo / "jobs" / "mcp_mode_probe.py",
                owner_user=os.environ.get("USER") or "pytest",
                server_instance_id=server_id,
                remote_pid=None,
                reserved_at="2026-05-30T12:10:00Z",
                last_heartbeat_at="2026-05-30T12:10:00Z",
            ),
        )
        reservations.atomic_write_json(
            job_record_path,
            reservations.build_job_record(
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key,
                host="localhost",
                gpu_index=7,
                script_path=repo / "jobs" / "mcp_mode_probe.py",
                args=[],
                output_file=repo / ".gpu_mcp_logs" / "phase6_probe.log",
                server_instance_id=server_id,
                next_poll_after="2026-05-30T12:11:00Z",
                created_at="2026-05-30T12:10:00Z",
            ),
        )

        result = _run_codex_exec(
            repo=repo,
            output_name=str(tmp_path / "codex_exec_installed_hook_context_probe.txt"),
            env={
                "GPU_MCP_TEST_RESERVATION_ROOT": str(registry),
                "GPU_MCP_TEST_NOW": "2099-01-01T00:00:00Z",
            },
            prompt=(
                "Do not run shell commands. Do not edit files. Use the MCP tool "
                "gpu-cluster-mcp/check_gpu_processes. Then report the exact tool "
                "result and any hook additional context you saw."
            ),
        )

        assert "status check" in result
        assert job_id in result
        assert reservation_key in result
        assert reminder_path.exists()
    finally:
        reminder_lock_path = reminder_path.with_suffix(".lock")
        for path in (
            job_record_path,
            reminder_path,
            reminder_lock_path,
            reservation_dir / "metadata.json",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        for directory in (job_record_path.parent, reservation_dir, registry):
            try:
                directory.rmdir()
            except OSError:
                pass
