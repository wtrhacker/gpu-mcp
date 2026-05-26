from __future__ import annotations

"""Contracts for the Codex PostToolUse policy-drift hook."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "gpu_mcp_policy_hook.py"


def _write_config(repo: Path) -> Path:
    (repo / "jobs").mkdir()
    (repo / "results").mkdir()
    (repo / ".gpu_mcp_logs").mkdir()
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


def test_policy_hook_exits_quietly_for_approved_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(repo, store_path=store)

    assert result is None


def test_policy_hook_main_prints_nothing_for_approved_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({"hook_event_name": "PostToolUse", "cwd": str(repo)}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_policy_hook_finds_policy_from_subdirectory_and_blocks_stale_policy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    nested = repo / "jobs" / "nested"
    nested.mkdir()
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(nested, store_path=store)

    assert result is not None
    assert result["decision"] == "block"
    assert "continue" not in result
    assert "hookSpecificOutput" not in result
    reason = result["reason"]
    assert "gpu-mcp.toml has changed but is not active" in reason
    assert "Do not revert it" in reason
    assert "If the human edited this file" in reason
    assert "If you edited this file" in reason
    assert "Only edit gpu-mcp.toml while stale after explicit human rejection" in reason
    assert "preview_policy_reload" in reason
    assert "only if they want to proceed" not in reason


def test_policy_hook_allows_reload_tools_while_policy_is_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    for tool_name in (
        "mcp__gpu_cluster_mcp__preview_policy_reload",
        "mcp__gpu_cluster_mcp__reload_policy",
        "mcp__gpu_cluster_mcp__reject_policy_reload",
    ):
        assert hook.check_policy_drift(repo, store_path=store, tool_name=tool_name) is None


def test_policy_hook_allows_narrow_policy_file_edit_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    allowed = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(config)},
    )
    blocked = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(repo / "README.md")},
    )

    assert allowed is None
    assert blocked is not None
    assert blocked["decision"] == "block"


def test_policy_hook_allows_patch_event_for_policy_file_only_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    allowed = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="functions.apply_patch",
        tool_input={
            "patch": "*** Begin Patch\n*** Update File: gpu-mcp.toml\n@@\n*** End Patch\n"
        },
    )
    blocked = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="functions.apply_patch",
        tool_input={
            "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n*** End Patch\n"
        },
    )

    assert allowed is None
    assert blocked is not None
    assert blocked["decision"] == "block"


def test_policy_hook_main_allows_top_level_patch_command_for_policy_file_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({
            "hook_event_name": "PreToolUse",
            "cwd": str(repo),
            "tool_name": "apply_patch",
            "command": "*** Begin Patch\n*** Update File: gpu-mcp.toml\n@@\n*** End Patch\n",
        }),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_policy_hook_allows_raw_string_patch_input_for_policy_file_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    allowed = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="apply_patch",
        tool_input="*** Begin Patch\n*** Update File: gpu-mcp.toml\n@@\n*** End Patch\n",
    )
    blocked = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="apply_patch",
        tool_input="*** Begin Patch\n*** Update File: gpu-mcp.toml\n*** Update File: README.md\n@@\n*** End Patch\n",
    )

    assert allowed is None
    assert blocked is not None
    assert blocked["decision"] == "block"


def test_policy_hook_search_is_bounded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    deep = repo
    for index in range(40):
        deep = deep / f"level_{index}"
    deep.mkdir(parents=True)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    assert hook.find_policy_file(deep, max_depth=8) is None
    assert hook.find_policy_file(deep, max_depth=64) == config


def test_policy_hook_main_reads_codex_json_event(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({"hook_event_name": "PostToolUse", "cwd": str(repo)}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["decision"] == "block"
    assert "gpu-mcp.toml has changed but is not active" in result["reason"]
