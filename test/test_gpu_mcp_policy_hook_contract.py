from __future__ import annotations

"""Contracts for the Codex PostToolUse policy-drift hook."""

import importlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "gpu_mcp_policy_hook.py"
CADENCE_POLICY = importlib.import_module("gpu_mcp_reservations")


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


def _approve_policy(config: Path, store: Path) -> None:
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])


def _seed_hook_job(
    repo: Path,
    registry: Path,
    *,
    next_poll_after: str,
    job_id: str = "job-20260530T123456Z-hookjob",
    attempt_id: str = "attempt-20260530T123456Z-hookattempt",
    server_instance_id: str = "server-gpu-a-1234-hookserver",
    gpu_index: int = 0,
    poll_interval_sec: int = 600,
):
    reservations = importlib.import_module("gpu_mcp_reservations")
    script = repo / "jobs" / "hook_job.py"
    script.write_text("print('hook job')\n")
    reservation_key = reservations.reservation_key("gpu-a", gpu_index)
    job_record = reservations.build_job_record(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key,
        host="gpu-a",
        gpu_index=gpu_index,
        script_path=script,
        args=[],
        output_file=repo / ".gpu_mcp_logs" / "hook.log",
        server_instance_id=server_instance_id,
        next_poll_after=next_poll_after,
    )
    job_record["poll_interval_sec"] = poll_interval_sec
    metadata = reservations.build_shared_metadata(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key,
        host="gpu-a",
        gpu_index=gpu_index,
        repo=repo,
        script_path=script,
        owner_user="tester",
        server_instance_id=server_instance_id,
        remote_pid=12345,
        process_fingerprint="gpu-mcp-process:hook",
    )
    reservations.atomic_write_json(reservations.job_record_path(repo, job_id), job_record)
    reservations.acquire_reservation(
        registry_root=registry,
        reservation_key_value=reservation_key,
        metadata=metadata,
    )
    return job_record


def _write_hook_outcome(repo: Path, job_record: dict, content: str = "{}\n") -> Path:
    reservations = importlib.import_module("gpu_mcp_reservations")
    outcome_path = reservations.outcome_record_path(
        repo,
        job_record["job_id"],
        job_record["active_attempt_id"],
    )
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    outcome_path.write_text(content, encoding="utf-8")
    return outcome_path


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


def test_policy_hook_bootstrap_allows_exact_recovery_names_but_not_suffix_spoofs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    store = tmp_path / "approved-policies.json"
    hook = importlib.import_module("gpu_mcp_policy_hook")

    for tool_name in (
        "mcp__gpu_cluster_mcp__preview_policy_reload",
        "mcp__gpu_cluster_mcp__reload_policy",
        "mcp__gpu_cluster_mcp__reject_policy_reload",
    ):
        assert hook.check_policy_drift(repo, store_path=store, tool_name=tool_name) is None

    spoofed = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="mcp__untrusted__silently_reload_policy",
    )
    assert spoofed is not None
    assert spoofed["decision"] == "block"
    assert "not been activated yet" in spoofed["reason"]

    bare = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="reload_policy",
    )
    assert bare is not None
    assert bare["decision"] == "block"


def test_policy_hook_allows_only_repo_codex_config_repair_during_bootstrap(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    store = tmp_path / "approved-policies.json"
    hook = importlib.import_module("gpu_mcp_policy_hook")
    codex_config = repo / ".codex" / "config.toml"

    allowed = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="functions.apply_patch",
        tool_input={
            "patch": "*** Begin Patch\n*** Add File: .codex/config.toml\n+x\n*** End Patch",
        },
    )
    exact_edit = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(codex_config)},
    )
    blocked = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(repo / "README.md")},
    )

    assert allowed is None
    assert exact_edit is None
    assert blocked is not None
    assert blocked["decision"] == "block"


def test_policy_hook_blocks_bootstrap_config_edits_through_symlinked_codex_dir(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".codex").symlink_to(outside, target_is_directory=True)
    store = tmp_path / "approved-policies.json"
    hook = importlib.import_module("gpu_mcp_policy_hook")

    attempts = (
        (
            "Edit",
            {"file_path": str(repo / ".codex" / "config.toml")},
        ),
        (
            "functions.apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n*** Add File: .codex/config.toml\n"
                    "+x\n*** End Patch"
                ),
            },
        ),
        (
            "functions.apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n*** Update File: .codex/config.toml\n"
                    "@@\n*** End Patch"
                ),
            },
        ),
    )

    for tool_name, tool_input in attempts:
        result = hook.check_policy_drift(
            repo,
            store_path=store,
            tool_name=tool_name,
            tool_input=tool_input,
        )
        assert result is not None
        assert result["decision"] == "block"


def test_policy_hook_blocks_bootstrap_edit_of_symlinked_codex_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    (repo / ".codex").mkdir()
    outside_config = tmp_path / "outside-config.toml"
    outside_config.write_text("outside = true\n")
    (repo / ".codex" / "config.toml").symlink_to(outside_config)
    store = tmp_path / "approved-policies.json"
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(repo / ".codex" / "config.toml")},
    )

    assert result is not None
    assert result["decision"] == "block"


def test_policy_hook_blocks_codex_config_edit_after_approved_policy_drifts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(repo / ".codex" / "config.toml")},
    )

    assert result is not None
    assert result["decision"] == "block"
    assert "changed but is not active" in result["reason"]


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


def test_policy_hook_allows_path_key_for_policy_file_edit_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    assert hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"path": str(config)},
    ) is None


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


def test_policy_hook_refuses_add_or_delete_patch_for_policy_file_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    for patch in (
        "*** Begin Patch\n*** Add File: gpu-mcp.toml\n+schema_version = 1\n*** End Patch\n",
        "*** Begin Patch\n*** Delete File: gpu-mcp.toml\n*** End Patch\n",
    ):
        result = hook.check_policy_drift(
            repo,
            store_path=store,
            tool_name="apply_patch",
            tool_input=patch,
        )
        assert result is not None
        assert result["decision"] == "block"


def test_policy_hook_refuses_move_patch_for_policy_file_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="apply_patch",
        tool_input=(
            "*** Begin Patch\n"
            "*** Update File: gpu-mcp.toml\n"
            "*** Move to: gpu-mcp.renamed.toml\n"
            "@@\n"
            "*** End Patch\n"
        ),
    )

    assert result is not None
    assert result["decision"] == "block"


def test_policy_hook_blocks_symlinked_policy_instead_of_target_edit(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = _write_config(outside)
    store = tmp_path / "approved-policies.json"
    _approve_policy(outside_config, store)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gpu-mcp.toml").symlink_to(outside_config)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_policy_drift(
        repo,
        store_path=store,
        tool_name="Edit",
        tool_input={"file_path": str(outside_config)},
    )

    assert result is not None
    assert result["decision"] == "block"
    assert "symlink" in result["reason"]


def test_policy_hook_blocks_symlinked_policy_even_for_reload_tools(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = _write_config(outside)
    store = tmp_path / "approved-policies.json"
    _approve_policy(outside_config, store)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gpu-mcp.toml").symlink_to(outside_config)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    for tool_name in (
        "mcp__gpu_cluster_mcp__preview_policy_reload",
        "mcp__gpu_cluster_mcp__reload_policy",
        "mcp__gpu_cluster_mcp__reject_policy_reload",
    ):
        result = hook.check_policy_drift(repo, store_path=store, tool_name=tool_name)
        assert result is not None
        assert result["decision"] == "block"
        assert "symlink" in result["reason"]


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


def test_policy_hook_allows_cmd_and_command_patch_inputs_for_policy_file_while_stale(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    for key in ("cmd", "command"):
        assert hook.check_policy_drift(
            repo,
            store_path=store,
            tool_name="apply_patch",
            tool_input={key: "*** Begin Patch\n*** Update File: gpu-mcp.toml\n@@\n*** End Patch\n"},
        ) is None


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
    assert "gpu-mcp.toml has not been activated yet" in result["reason"]
    assert "candidate_summary" in result["reason"]



def test_phase6_test_capability_nonce_emits_additional_context_only_under_pytest():
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_test_hook_capability_nonce(
        env={
            "PYTEST_CURRENT_TEST": "phase6 capability",
            "GPU_MCP_TEST_HOOK_CAPABILITY_NONCE": "phase6-hook-nonce-contract",
        }
    )
    production = hook.check_test_hook_capability_nonce(
        env={"GPU_MCP_TEST_HOOK_CAPABILITY_NONCE": "phase6-hook-nonce-contract"}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert result["hookSpecificOutput"]["additionalContext"] == "phase6-hook-nonce-contract"
    assert production is None


@pytest.mark.parametrize("registration", ["old_name", "launcher", "malformed", "unreadable", "missing"])
@pytest.mark.parametrize("event_name", ["PreToolUse", "Stop"])
def test_hooks_never_poll_repository_jobs_for_any_registration(tmp_path, monkeypatch, capsys, registration, event_name):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approvals.json"
    _approve_policy(config, store)
    registry = tmp_path / "reservations"
    record = _seed_hook_job(repo, registry, next_poll_after="2000-01-01T00:00:00Z")
    _write_hook_outcome(repo, record)
    path = repo / ".codex/config.toml"
    path.parent.mkdir()
    if registration in {"old_name", "launcher"}:
        entry = "gpu_mcp_server.py" if registration == "old_name" else "gpu_mcp_codex_launcher.py"
        path.write_text(f"[mcp_servers.gpu-cluster-mcp]\nargs = ['/installed/{entry}', '--config', {str(config)!r}]\n")
    elif registration == "malformed":
        path.write_text("[broken toml")
    elif registration == "unreadable":
        path.mkdir()
    before = CADENCE_POLICY.job_record_path(repo, record["job_id"]).read_bytes()
    monkeypatch.setattr(CADENCE_POLICY, "repo_job_state_root", lambda *a: pytest.fail("hook scanned repository jobs"))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({
        "hook_event_name": event_name, "cwd": str(repo), "session_id": "another-session", "turn_id": "turn",
    })))
    hook = importlib.import_module("gpu_mcp_policy_hook")
    assert hook.main(["--store", str(store)]) == 0
    assert capsys.readouterr().out == ""
    assert (repo / ".gpu_mcp_state/jobs" / record["job_id"] / "job.json").read_bytes() == before
