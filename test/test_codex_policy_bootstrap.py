from __future__ import annotations

"""Opt-in live Codex coverage for first-policy bootstrap quarantine."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "gpu_mcp_server.py"
MCP_NAME = "gpu-bootstrap-probe"


live_codex_exec = pytest.mark.skipif(
    os.environ.get("GPU_MCP_RUN_CODEX_EXEC_TESTS") != "1",
    reason="set GPU_MCP_RUN_CODEX_EXEC_TESTS=1 to run live codex exec probes",
)


def _write_bootstrap_repo(repo: Path) -> Path:
    (repo / ".codex").mkdir(parents=True)
    (repo / "jobs").mkdir()
    (repo / "results").mkdir()
    (repo / ".gpu_mcp_logs").mkdir()
    subprocess.run(
        ["git", "init", str(repo)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    policy = repo / "gpu-mcp.toml"
    policy.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                "nodes = ['gpu-bootstrap-test.invalid']",
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
    store = repo / ".gpu_mcp_state" / "approved-policies.json"
    (repo / ".codex" / "config.toml").write_text(
        "\n".join(
            [
                f"[mcp_servers.{MCP_NAME}]",
                f"command = {sys.executable!r}",
                f"args = [{str(SERVER)!r}, '--config', {str(policy)!r}]",
                f"cwd = {str(repo)!r}",
                "enabled = true",
                "startup_timeout_sec = 20",
                "tool_timeout_sec = 30",
                "",
                f"[mcp_servers.{MCP_NAME}.env]",
                f"GPU_MCP_TEST_POLICY_APPROVAL_STORE = {str(store)!r}",
                'PYTEST_CURRENT_TEST = "codex-policy-bootstrap"',
                "",
                f"[mcp_servers.{MCP_NAME}.tools.preview_policy_reload]",
                'approval_mode = "approve"',
                f"[mcp_servers.{MCP_NAME}.tools.reject_policy_reload]",
                'approval_mode = "approve"',
                f"[mcp_servers.{MCP_NAME}.tools.reload_policy]",
                'approval_mode = "prompt"',
                f"[mcp_servers.{MCP_NAME}.tools.check_gpus]",
                'approval_mode = "approve"',
                "",
            ]
        )
    )
    return store


def _run_codex(repo: Path, prompt: str, output_name: str) -> str:
    output_path = repo / output_name
    child_env = os.environ.copy()
    child_env.pop("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", None)
    completed = subprocess.run(
        [
            "codex",
            "--disable",
            "hooks",
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            str(repo),
            "-c",
            f"projects.{json.dumps(str(repo))}.trust_level=\"trusted\"",
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(output_path),
            prompt,
        ],
        cwd=REPO_ROOT,
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    return output_path.read_text()


def _extract_json_object(text: str, *, expected_status: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("status") == expected_status:
            return value
    raise AssertionError(f"no JSON object with status={expected_status!r} found:\n{text}")


@pytest.mark.codex_exec
@live_codex_exec
def test_live_codex_agent_can_preview_first_policy_without_out_of_band_approval(tmp_path):
    repo = tmp_path / "bootstrap-repo"
    store = _write_bootstrap_repo(repo)

    final_message = _run_codex(
        repo,
        (
            "Do not run shell commands and do not edit files. Call only the MCP tool "
            f"{MCP_NAME}/preview_policy_reload. Return its raw JSON result exactly, "
            "without Markdown or commentary."
        ),
        "codex_bootstrap_preview.txt",
    )

    preview = _extract_json_object(final_message, expected_status="preview")
    assert preview["approval_state"] == "bootstrap_pending"
    assert preview["activation_mode"] == "bootstrap"
    assert preview["active_hash"] is None
    assert preview["candidate_summary"]["nodes"] == ["gpu-bootstrap-test.invalid"]
    assert preview["reload_token"].startswith("gpu-mcp-reload-v1:")
    assert not store.exists()


@pytest.mark.codex_exec
@live_codex_exec
def test_live_codex_agent_gets_server_side_refusal_for_bootstrap_operation(tmp_path):
    repo = tmp_path / "bootstrap-repo"
    store = _write_bootstrap_repo(repo)

    final_message = _run_codex(
        repo,
        (
            "Do not run shell commands and do not edit files. Call only the MCP tool "
            f"{MCP_NAME}/check_gpus with samples=1 and threshold=10. Return its raw "
            "JSON result exactly, without Markdown or commentary."
        ),
        "codex_bootstrap_refusal.txt",
    )

    refusal = _extract_json_object(final_message, expected_status="refused")
    assert refusal["policy_state"] == "bootstrap_pending"
    assert "All GPU, SSH, process, reservation" in refusal["reason"]
    assert not store.exists()
