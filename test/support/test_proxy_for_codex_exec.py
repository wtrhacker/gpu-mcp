from __future__ import annotations

import json
from pathlib import Path

import pytest

import proxy_for_codex_exec


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "test_mcp_repos"


def _write_repo(name: str, host: str, marker: str) -> tuple[Path, Path]:
    repo = FIXTURE_ROOT / name
    jobs = repo / "jobs"
    results = repo / "results"
    jobs.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)

    job = jobs / "ok_job.py"
    job.write_text(
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

    config = repo / "gpu-mcp.toml"
    config.write_text(
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
    return repo, config


def test_proxy_for_codex_exec_keeps_two_repo_policies_separate():
    repo_a, config_a = _write_repo("repo_a", "gpu-a", "repo-a-ran")
    repo_b, config_b = _write_repo("repo_b", "gpu-b", "repo-b-ran")

    server_a = proxy_for_codex_exec.ProxyForCodexExec.from_config(config_a)
    server_b = proxy_for_codex_exec.ProxyForCodexExec.from_config(config_b)

    result_a = server_a.run_python_on_gpu("gpu-a", "jobs/ok_job.py")
    result_b = server_b.run_python_on_gpu("gpu-b", "jobs/ok_job.py")

    assert result_a["status"] == "ok"
    assert result_a["host"] == "gpu-a"
    assert result_a["repo_root"] == str(repo_a)
    assert "repo-a-ran" in result_a["stdout"]
    assert (repo_a / "results" / "marker.txt").read_text() == "repo-a-ran"

    assert result_b["status"] == "ok"
    assert result_b["host"] == "gpu-b"
    assert result_b["repo_root"] == str(repo_b)
    assert "repo-b-ran" in result_b["stdout"]
    assert (repo_b / "results" / "marker.txt").read_text() == "repo-b-ran"

    with pytest.raises(proxy_for_codex_exec.PolicyError, match="host not allowed"):
        server_a.run_python_on_gpu("gpu-b", "jobs/ok_job.py")

    with pytest.raises(proxy_for_codex_exec.PolicyError, match="script outside approved roots"):
        server_a.run_python_on_gpu("gpu-a", str(repo_b / "jobs" / "ok_job.py"))


def test_proxy_for_codex_exec_cli_uses_explicit_config_only():
    repo_a, config_a = _write_repo("repo_a", "gpu-a", "repo-a-ran")

    result = proxy_for_codex_exec.run_once_for_cli(
        config_path=config_a,
        host="gpu-a",
        script_path="jobs/ok_job.py",
    )

    parsed = json.loads(result)
    assert parsed["status"] == "ok"
    assert parsed["repo_root"] == str(repo_a)
