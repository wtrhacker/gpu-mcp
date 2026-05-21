from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos"
SERVER = REPO_ROOT / "test" / "support" / "proxy_for_codex_exec.py"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _server_config(repo_name: str) -> tuple[Path, dict]:
    repo = FIXTURE_ROOT / repo_name
    config_path = repo / ".codex" / "config.toml"
    config = _load_toml(config_path)
    return repo, config


def test_repo_local_codex_configs_point_to_matching_gpu_mcp_policy():
    repo_a, config_a = _server_config("repo_a")
    repo_b, config_b = _server_config("repo_b")

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


def test_repo_local_gpu_mcp_policies_are_distinct():
    repo_a = FIXTURE_ROOT / "repo_a"
    repo_b = FIXTURE_ROOT / "repo_b"

    policy_a = _load_toml(repo_a / "gpu-mcp.toml")
    policy_b = _load_toml(repo_b / "gpu-mcp.toml")

    assert policy_a["repo_root"] == str(repo_a)
    assert policy_b["repo_root"] == str(repo_b)
    assert policy_a["nodes"] == ["gpu-a"]
    assert policy_b["nodes"] == ["gpu-b"]


def _run_codex_exec(repo: Path, prompt: str, output_name: str) -> str:
    output_path = repo / output_name
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "-C",
            str(repo),
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(output_path),
            prompt,
        ],
        cwd=REPO_ROOT,
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
def test_codex_exec_loads_repo_a_local_mcp_config():
    repo_a = FIXTURE_ROOT / "repo_a"

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
def test_codex_exec_loads_repo_b_local_mcp_config():
    repo_b = FIXTURE_ROOT / "repo_b"

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
def test_codex_exec_repo_a_rejects_repo_b_host():
    repo_a = FIXTURE_ROOT / "repo_a"

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
