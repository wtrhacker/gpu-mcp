from __future__ import annotations

"""Executable behavior contract for the config-driven GPU MCP server.

These tests intentionally target the future public API, not the old hard-coded
`gpu_mcp_server.py` globals. They should stay xfailed until the real
config-driven implementation exists, then the xfail marker should be removed.

Expected future API:

- `gpu_mcp_config.load_policy(config_path) -> policy`
- `gpu_mcp_config.ConfigError`
- `gpu_mcp_policy.PolicyError`
- `gpu_mcp_policy.validate_host(policy, host)`
- `gpu_mcp_policy.resolve_script_path(policy, script_path)`
- `gpu_mcp_policy.validate_write_path(policy, path)`
- `gpu_mcp_policy.validate_output_path(policy, path)`
- `gpu_mcp_policy.scan_python_script_safety(path) -> list[str]`
- `gpu_mcp_policy.run_python_on_gpu(policy, host, script_path, ...) -> dict`
"""

import importlib
import json
import uuid
from pathlib import Path

import jsonschema
import pytest


pytestmark = [
    pytest.mark.contract,
    pytest.mark.xfail(
        reason="config-driven GPU MCP policy implementation is not written yet",
        strict=True,
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos" / "policy_contract"
MCP_RESULT_SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "schemas" / "mcp-result.schema.json").read_text()
)


@pytest.fixture()
def policy_modules():
    return (
        importlib.import_module("gpu_mcp_config"),
        importlib.import_module("gpu_mcp_policy"),
    )


@pytest.fixture()
def repo_fixture(request):
    name = f"{request.node.name}-{uuid.uuid4().hex}"
    repo = FIXTURE_ROOT / name / "repo"
    jobs = repo / "jobs"
    results = repo / "results"
    logs = repo / ".gpu_mcp_logs"
    jobs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return repo


def _write_config(
    repo: Path,
    *,
    nodes: list[str] | None = None,
    script_roots: list[str] | None = None,
    write_roots: list[str] | None = None,
    output_roots: list[str] | None = None,
    sync_timeout_sec: int = 5,
) -> Path:
    config = repo / "gpu-mcp.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                f"nodes = {nodes or ['gpu-a']!r}",
                f"script_roots = {script_roots or ['jobs']!r}",
                f"write_roots = {write_roots or ['results']!r}",
                f"output_roots = {output_roots or ['.gpu_mcp_logs']!r}",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                f"sync_timeout_sec = {sync_timeout_sec}",
                "",
            ]
        )
    )
    return config


def _write_job(repo: Path, name: str, source: str) -> Path:
    path = repo / "jobs" / name
    path.write_text(source)
    return path


def _assert_mcp_result(result: dict, *, status: str, code: str) -> None:
    jsonschema.validate(instance=result, schema=MCP_RESULT_SCHEMA)
    assert result["schema_version"] == 1
    assert result["status"] == status
    assert result["code"] == code
    assert isinstance(result["message"], str)
    assert isinstance(result["details"], dict)


def test_config_requires_absolute_config_path(policy_modules, repo_fixture):
    gpu_mcp_config, _ = policy_modules
    _write_config(repo_fixture)

    with pytest.raises(gpu_mcp_config.ConfigError, match="absolute"):
        gpu_mcp_config.load_policy(Path("gpu-mcp.toml"))


def test_config_rejects_repo_root_that_does_not_match_config_location(
    policy_modules, repo_fixture
):
    gpu_mcp_config, _ = policy_modules
    config = _write_config(repo_fixture)
    config.write_text(
        config.read_text().replace(
            f"repo_root = {str(repo_fixture)!r}",
            f"repo_root = {str(repo_fixture.parent / 'other_repo')!r}",
        )
    )

    with pytest.raises(gpu_mcp_config.ConfigError, match="repo_root"):
        gpu_mcp_config.load_policy(config)


def test_config_rejects_script_root_outside_repo_even_under_tmp(
    policy_modules, repo_fixture
):
    gpu_mcp_config, _ = policy_modules
    config = _write_config(repo_fixture, script_roots=["/tmp/gpu_mcp_jobs"])

    with pytest.raises(gpu_mcp_config.ConfigError, match="script_roots"):
        gpu_mcp_config.load_policy(config)


def test_config_rejects_non_tmp_write_root_outside_repo(policy_modules, repo_fixture):
    gpu_mcp_config, _ = policy_modules
    config = _write_config(repo_fixture, write_roots=["/home/user/outside_results"])

    with pytest.raises(gpu_mcp_config.ConfigError, match="write_roots"):
        gpu_mcp_config.load_policy(config)


def test_config_allows_explicit_tmp_write_root(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    tmp_root = f"/tmp/gpu_mcp_policy_contract_{uuid.uuid4().hex}"
    config = _write_config(repo_fixture, write_roots=[tmp_root])
    policy = gpu_mcp_config.load_policy(config)

    validated = gpu_mcp_policy.validate_write_path(policy, f"{tmp_root}/out.txt")

    assert validated == Path(f"{tmp_root}/out.txt")


def test_config_rejects_non_tmp_output_root_outside_repo(policy_modules, repo_fixture):
    gpu_mcp_config, _ = policy_modules
    config = _write_config(repo_fixture, output_roots=["/home/user/outside_logs"])

    with pytest.raises(gpu_mcp_config.ConfigError, match="output_roots"):
        gpu_mcp_config.load_policy(config)


def test_host_allowlist_rejects_repo_excluded_host(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, nodes=["gpu-a"])
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="host not allowed"):
        gpu_mcp_policy.validate_host(policy, "gpu-b")


def test_script_path_escape_is_rejected(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, script_roots=["jobs"])
    outside = repo_fixture.parent / "outside.py"
    outside.write_text("print('outside')\n")
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="script"):
        gpu_mcp_policy.resolve_script_path(policy, "../outside.py")


def test_symlink_escape_from_script_root_is_rejected(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, script_roots=["jobs"])
    outside = repo_fixture.parent / "outside.py"
    outside.write_text("print('outside')\n")
    link = repo_fixture / "jobs" / "link.py"
    link.symlink_to(outside)
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="script"):
        gpu_mcp_policy.resolve_script_path(policy, "jobs/link.py")


def test_allowed_write_root_succeeds(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, write_roots=["results"])
    policy = gpu_mcp_config.load_policy(config)

    validated = gpu_mcp_policy.validate_write_path(policy, "results/sentinel.txt")

    assert validated == repo_fixture / "results" / "sentinel.txt"


def test_forbidden_write_root_fails(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, write_roots=["results"])
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="write"):
        gpu_mcp_policy.validate_write_path(policy, "../outside_sentinel.txt")


def test_sqlite_database_outside_write_roots_is_rejected(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, write_roots=["results"])
    policy = gpu_mcp_config.load_policy(config)
    db_path = repo_fixture.parent / "outside.db"

    with pytest.raises(gpu_mcp_policy.PolicyError, match="write"):
        gpu_mcp_policy.validate_write_path(policy, db_path, purpose="sqlite")

    assert not db_path.exists()


def test_async_output_file_must_be_under_output_roots(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, output_roots=[".gpu_mcp_logs"])
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="output"):
        gpu_mcp_policy.validate_output_path(policy, "jobs/ok_job.py")


def test_sync_timeout_returns_server_timeout(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture, sync_timeout_sec=1)
    _write_job(
        repo_fixture,
        "sleep_10.py",
        "import time\n"
        "time.sleep(10)\n"
        "print('finished')\n",
    )
    policy = gpu_mcp_config.load_policy(config)

    result = gpu_mcp_policy.run_python_on_gpu(
        policy,
        host="gpu-a",
        script_path="jobs/sleep_10.py",
        async_mode=False,
    )

    _assert_mcp_result(result, status="error", code="timeout")


def test_subprocess_import_is_rejected_before_execution(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture)
    script = _write_job(
        repo_fixture,
        "subprocess_job.py",
        "import subprocess\n"
        "subprocess.run(['python', '--version'])\n",
    )
    policy = gpu_mcp_config.load_policy(config)

    issues = gpu_mcp_policy.scan_python_script_safety(script)

    assert any("subprocess" in issue for issue in issues)
    with pytest.raises(gpu_mcp_policy.PolicyError, match="subprocess"):
        gpu_mcp_policy.run_python_on_gpu(policy, "gpu-a", "jobs/subprocess_job.py")


def test_dynamic_import_is_rejected(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture)
    script = _write_job(
        repo_fixture,
        "dynamic_import_job.py",
        "__import__('subprocess')\n",
    )
    policy = gpu_mcp_config.load_policy(config)

    issues = gpu_mcp_policy.scan_python_script_safety(script)

    assert any("__import__" in issue or "dynamic" in issue for issue in issues)
    with pytest.raises(gpu_mcp_policy.PolicyError):
        gpu_mcp_policy.run_python_on_gpu(policy, "gpu-a", "jobs/dynamic_import_job.py")


def test_socket_connect_is_rejected(policy_modules, repo_fixture):
    gpu_mcp_config, gpu_mcp_policy = policy_modules
    config = _write_config(repo_fixture)
    _write_job(
        repo_fixture,
        "socket_job.py",
        "import socket\n"
        "socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n",
    )
    policy = gpu_mcp_config.load_policy(config)

    result = gpu_mcp_policy.run_python_on_gpu(policy, "gpu-a", "jobs/socket_job.py")

    assert result["code"] in {"policy_violation", "runtime_policy_violation"}
    _assert_mcp_result(result, status="error", code=result["code"])
