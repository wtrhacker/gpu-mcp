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


def test_phase6_due_reminder_emits_additional_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.splitlines() == [
        "GPU MCP: 1 managed job has a scheduled status check due.",
        (
            f'- {job_record["job_id"]}: scheduled status check is overdue by 5m; '
            f'call manage_gpu_job(action="status", job_id="{job_record["job_id"]}").'
        ),
        "Continue from the returned lifecycle.",
    ]


@pytest.mark.parametrize("target_field", ["job_id", "reservation_key"])
@pytest.mark.parametrize("event_kind", ["poll_due", "outcome"])
def test_pretool_reminder_suppresses_job_covered_by_inflight_status(
    tmp_path,
    monkeypatch,
    target_field,
    event_kind,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    job = _seed_hook_job(
        repo,
        registry,
        next_poll_after=(
            "2026-05-30T12:00:00Z"
            if event_kind == "poll_due"
            else "2099-01-01T00:00:00Z"
        ),
    )
    if event_kind == "outcome":
        _write_hook_outcome(repo, job)
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder_path = reservations.hook_reminder_path(repo, job["job_id"])
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        tool_name="mcp__repo_managed_gpu__manage_gpu_job",
        tool_input={"action": "status", target_field: job[target_field]},
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is None
    assert not reminder_path.exists()


def test_pretool_targeted_status_suppresses_only_the_covered_due_job(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    covered = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-covered",
        attempt_id="attempt-20260530T123456Z-covered",
        gpu_index=0,
    )
    remaining = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-remaining",
        attempt_id="attempt-20260530T123456Z-remaining",
        gpu_index=1,
    )
    reservations = importlib.import_module("gpu_mcp_reservations")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        tool_name="repo_managed_gpu/manage_gpu_job",
        tool_input={"action": "status", "job_id": covered["job_id"]},
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert covered["job_id"] not in context
    assert remaining["job_id"] in context
    assert not reservations.hook_reminder_path(repo, covered["job_id"]).exists()
    assert reservations.hook_reminder_path(repo, remaining["job_id"]).exists()


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


def test_phase6_due_reminder_mode_defaults_to_additional_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.delenv("GPU_MCP_HOOK_REMINDER_MODE", raising=False)
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    assert job_record["job_id"] in result["hookSpecificOutput"]["additionalContext"]


def test_phase6_due_reminder_explicit_off_mode_disables(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "off")
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is None


def test_phase6_due_reminder_is_silent_inside_time_throttle_window(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    hook = importlib.import_module("gpu_mcp_policy_hook")
    now = datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)

    first = hook.check_gpu_job_reminders(repo, now=now)
    second = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 14, 59, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is None
    reminder = json.loads(
        reservations.hook_reminder_path(repo, job_record["job_id"]).read_text()
    )
    assert reminder["last_reminded_at"] == "2026-05-30T12:05:00Z"
    assert reminder["reminder_count_for_poll_after"] == 1


def test_phase6_due_reminder_reemits_after_job_poll_interval(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        poll_interval_sec=600,
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")

    first = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )
    second = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 15, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is not None
    context = second["hookSpecificOutput"]["additionalContext"]
    assert job_record["job_id"] in context
    reminder_path = importlib.import_module("gpu_mcp_reservations").hook_reminder_path(
        repo, job_record["job_id"]
    )
    reminder = json.loads(reminder_path.read_text())
    assert reminder["last_reminded_poll_after"] == "2026-05-30T12:00:00Z"
    assert reminder["last_reminded_at"] == "2026-05-30T12:15:00Z"
    assert reminder["reminder_count_for_poll_after"] == 2


@pytest.mark.parametrize(
    ("record_value", "expected_interval_sec"),
    [
        (None, CADENCE_POLICY.DEFAULT_POLL_INTERVAL_SEC),
        ("fast", CADENCE_POLICY.DEFAULT_POLL_INTERVAL_SEC),
        (1, 1),
        (3 * CADENCE_POLICY.SECONDS_PER_HOUR, 3 * CADENCE_POLICY.SECONDS_PER_HOUR),
    ],
)
def test_phase6_due_reminder_interval_defaults_and_has_no_policy_cap(
    tmp_path,
    monkeypatch,
    record_value,
    expected_interval_sec,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        poll_interval_sec=600,
    )
    reservations = importlib.import_module("gpu_mcp_reservations")
    job_path = reservations.job_record_path(repo, "job-20260530T123456Z-hookjob")
    record = json.loads(job_path.read_text())
    if record_value is None:
        record.pop("poll_interval_sec", None)
    else:
        record["poll_interval_sec"] = record_value
    reservations.atomic_write_json(job_path, record)
    hook = importlib.import_module("gpu_mcp_policy_hook")
    first_at = datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)

    first = hook.check_gpu_job_reminders(repo, now=first_at)
    early = hook.check_gpu_job_reminders(
        repo,
        now=first_at + timedelta(seconds=expected_interval_sec - 1),
    )
    due_again = hook.check_gpu_job_reminders(
        repo,
        now=first_at + timedelta(seconds=expected_interval_sec),
    )

    assert first is not None
    assert early is None
    assert due_again is not None


def test_phase6_status_acknowledges_old_due_timestamp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    record_path = reservations.job_record_path(repo, job_record["job_id"])
    hook = importlib.import_module("gpu_mcp_policy_hook")

    first = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )
    updated = json.loads(record_path.read_text())
    updated["last_status_checked_at"] = "2026-05-30T12:06:00Z"
    reservations.atomic_write_json(record_path, updated)
    after_status = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 30, tzinfo=timezone.utc),
    )

    assert first is not None
    assert after_status is None


def test_phase6_status_before_due_does_not_acknowledge_due_timestamp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    record_path = reservations.job_record_path(repo, job_record["job_id"])
    updated = json.loads(record_path.read_text())
    updated["last_status_checked_at"] = "2026-05-30T11:59:59Z"
    reservations.atomic_write_json(record_path, updated)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    assert job_record["job_id"] in result["hookSpecificOutput"]["additionalContext"]


def test_phase6_old_status_check_does_not_silence_later_due_timestamp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:20:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    record_path = reservations.job_record_path(repo, job_record["job_id"])
    updated = json.loads(record_path.read_text())
    updated["last_status_checked_at"] = "2026-05-30T12:06:00Z"
    reservations.atomic_write_json(record_path, updated)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 30, tzinfo=timezone.utc),
    )

    assert result is not None
    assert job_record["job_id"] in result["hookSpecificOutput"]["additionalContext"]


def test_phase6_terminal_job_record_is_silent_even_if_reservation_remains(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    record_path = reservations.job_record_path(repo, job_record["job_id"])
    updated = json.loads(record_path.read_text())
    updated["job_lifecycle"] = "succeeded"
    updated["last_status_checked_at"] = "2026-05-30T12:06:00Z"
    updated["next_poll_after"] = None
    reservations.atomic_write_json(record_path, updated)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 30, tzinfo=timezone.utc),
    )

    assert result is None


def test_phase6_status_acknowledgement_is_per_job(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_a = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-acka",
        attempt_id="attempt-20260530T123456Z-acka",
        gpu_index=0,
    )
    job_b = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-ackb",
        attempt_id="attempt-20260530T123456Z-ackb",
        gpu_index=1,
    )
    reservations = importlib.import_module("gpu_mcp_reservations")
    record_a_path = reservations.job_record_path(repo, job_a["job_id"])
    updated_a = json.loads(record_a_path.read_text())
    updated_a["last_status_checked_at"] = "2026-05-30T12:06:00Z"
    reservations.atomic_write_json(record_a_path, updated_a)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 30, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert job_a["job_id"] not in context
    assert job_b["job_id"] in context


def test_phase6_due_reminder_lists_multiple_due_jobs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    old_job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T11:42:00Z",
        job_id="job-20260530T123456Z-old",
        attempt_id="attempt-20260530T123456Z-old",
        gpu_index=0,
    )
    new_job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T11:44:00Z",
        job_id="job-20260530T123456Z-new",
        attempt_id="attempt-20260530T123456Z-new",
        gpu_index=1,
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.splitlines() == [
        "GPU MCP: 2 managed jobs have scheduled status checks due.",
        (
            f'- {old_job["job_id"]}: scheduled status check is overdue by 18m; '
            f'call manage_gpu_job(action="status", job_id="{old_job["job_id"]}").'
        ),
        (
            f'- {new_job["job_id"]}: scheduled status check is overdue by 16m; '
            f'call manage_gpu_job(action="status", job_id="{new_job["job_id"]}").'
        ),
        "Continue from the returned lifecycle.",
    ]
    reservations = importlib.import_module("gpu_mcp_reservations")
    for job_record in (old_job, new_job):
        reminder = json.loads(
            reservations.hook_reminder_path(repo, job_record["job_id"]).read_text()
        )
        assert reminder["last_reminded_poll_after"] == job_record["next_poll_after"]
        assert reminder["reminder_count_for_poll_after"] == 1


def test_phase6_due_reminder_tie_breaks_by_job_id(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    z_job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T11:42:00Z",
        job_id="job-20260530T123456Z-zjob",
        attempt_id="attempt-20260530T123456Z-zjob",
        gpu_index=1,
    )
    a_job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T11:42:00Z",
        job_id="job-20260530T123456Z-ajob",
        attempt_id="attempt-20260530T123456Z-ajob",
        gpu_index=0,
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index(a_job["job_id"]) < context.index(z_job["job_id"])


def test_phase6_future_poll_and_posttooluse_are_silent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T13:00:00Z")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    direct = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert direct is None

    _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-duepost",
        attempt_id="attempt-20260530T123456Z-duepost",
        gpu_index=1,
    )
    reservations = importlib.import_module("gpu_mcp_reservations")
    due_reminder_path = reservations.hook_reminder_path(
        repo, "job-20260530T123456Z-duepost"
    )
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
    assert not due_reminder_path.exists()
    later_pretool = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )
    assert later_pretool is not None
    assert "job-20260530T123456Z-duepost" in later_pretool["hookSpecificOutput"]["additionalContext"]


def test_pretool_outcome_reminder_fires_before_future_poll_without_parsing(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    job = _seed_hook_job(repo, registry, next_poll_after="2099-01-01T00:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    job_path = reservations.job_record_path(repo, job["job_id"])
    metadata_path = registry / job["reservation_key"] / "metadata.json"
    _write_hook_outcome(repo, job, "{malformed outcome")
    job_before = job_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.splitlines()[0] == (
        "GPU MCP: 1 managed job has a local outcome to reconcile through status."
    )
    assert "local outcome detected" in context
    assert f'job_id="{job["job_id"]}"' in context
    assert job_path.read_bytes() == job_before
    assert metadata_path.read_bytes() == metadata_before


def test_pretool_outcome_reminder_is_throttled_per_attempt(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2099-01-01T00:00:00Z",
        poll_interval_sec=600,
    )
    _write_hook_outcome(repo, job)
    hook = importlib.import_module("gpu_mcp_policy_hook")
    first_at = datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)

    first = hook.check_gpu_job_reminders(repo, now=first_at)
    duplicate = hook.check_gpu_job_reminders(
        repo,
        now=first_at + timedelta(seconds=599),
    )
    repeated = hook.check_gpu_job_reminders(
        repo,
        now=first_at + timedelta(seconds=600),
    )

    assert first is not None
    assert duplicate is None
    assert repeated is not None
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder = json.loads(
        reservations.hook_reminder_path(repo, job["job_id"]).read_text()
    )
    assert reminder["last_outcome_attempt_id"] == job["active_attempt_id"]
    assert reminder["outcome_reminder_count"] == 2


def test_pretool_new_outcome_is_not_hidden_by_recent_poll_reminder(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    job = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        poll_interval_sec=600,
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")
    first_at = datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)

    poll_reminder = hook.check_gpu_job_reminders(repo, now=first_at)
    _write_hook_outcome(repo, job)
    outcome_reminder = hook.check_gpu_job_reminders(
        repo,
        now=first_at + timedelta(seconds=1),
    )

    assert poll_reminder is not None
    assert outcome_reminder is not None
    context = outcome_reminder["hookSpecificOutput"]["additionalContext"]
    assert "local outcome" in context
    assert "overdue by" not in context


def test_pretool_outcome_reminder_surfaces_during_other_tool_call(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_TEST_NOW", "2026-05-30T12:05:00Z")
    job = _seed_hook_job(repo, registry, next_poll_after="2099-01-01T00:00:00Z")
    _write_hook_outcome(repo, job)

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(repo),
                "tool_name": "mcp__gpu_cluster_mcp__run_python_on_gpu",
                "tool_input": {"async_mode": True, "job_role": "main"},
            }
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "local outcome" in context
    assert job["job_id"] in context


def test_phase6_due_reminder_filters_to_current_repo_jobs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other-repo"
    repo.mkdir()
    other_repo.mkdir()
    _write_config(repo)
    _write_config(other_repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T13:00:00Z", gpu_index=0)
    _seed_hook_job(
        other_repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-otherrepo",
        attempt_id="attempt-20260530T123456Z-otherrepo",
        gpu_index=1,
    )
    hook = importlib.import_module("gpu_mcp_policy_hook")

    current_repo_result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )
    other_repo_result = hook.check_gpu_job_reminders(
        other_repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert current_repo_result is None
    assert other_repo_result is not None
    assert "job-20260530T123456Z-otherrepo" in (
        other_repo_result["hookSpecificOutput"]["additionalContext"]
    )


def test_phase6_stale_policy_block_wins_over_due_reminder(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(repo)}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["decision"] == "block"
    assert "gpu-mcp.toml has changed" in result["reason"]
    assert "hookSpecificOutput" not in result


def test_phase6_malformed_reminder_state_recovers_and_reminds(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder_path = reservations.hook_reminder_path(repo, job_record["job_id"])
    reminder_path.parent.mkdir(parents=True)
    reminder_path.write_text("[]")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    assert job_record["job_id"] in result["hookSpecificOutput"]["additionalContext"]
    reminder = json.loads(reminder_path.read_text())
    assert reminder["last_reminded_poll_after"] == "2026-05-30T12:00:00Z"
    assert reminder["reminder_count_for_poll_after"] == 1


def test_phase6_malformed_reminder_state_does_not_starve_later_due_job(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    bad_record = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-badreminder",
        attempt_id="attempt-20260530T123456Z-badreminder",
        gpu_index=0,
    )
    good_record = _seed_hook_job(
        repo,
        registry,
        next_poll_after="2026-05-30T12:00:00Z",
        job_id="job-20260530T123456Z-goodreminder",
        attempt_id="attempt-20260530T123456Z-goodreminder",
        gpu_index=1,
    )
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder_path = reservations.hook_reminder_path(repo, bad_record["job_id"])
    reminder_path.parent.mkdir(parents=True)
    reminder_path.write_text("[]")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert bad_record["job_id"] in context
    assert good_record["job_id"] in context


def test_phase6_symlinked_job_directory_is_ignored(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    job_dir = reservations.job_record_path(repo, job_record["job_id"]).parent
    outside_job_dir = tmp_path / "outside-job-dir"
    job_dir.rename(outside_job_dir)
    job_dir.symlink_to(outside_job_dir, target_is_directory=True)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is None


def test_phase6_reminder_claim_is_cross_process_exclusive(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    start_file = tmp_path / "start"
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    code = f"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import gpu_mcp_policy_hook as hook

index = os.environ["GPU_MCP_CLAIM_WORKER_INDEX"]
(Path({str(ready_dir)!r}) / index).write_text("ready")
start = Path({str(start_file)!r})
while not start.exists():
    time.sleep(0.01)
result = hook.check_gpu_job_reminders(
    Path({str(repo)!r}),
    now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
)
print(json.dumps({{
    "reminded": result is not None,
    "context": None if result is None else result["hookSpecificOutput"]["additionalContext"],
}}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    procs = []
    for index in range(2):
        child_env = env.copy()
        child_env["GPU_MCP_CLAIM_WORKER_INDEX"] = str(index)
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=REPO_ROOT,
                env=child_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    deadline = datetime.now(timezone.utc).timestamp() + 5
    while len(list(ready_dir.iterdir())) < 2 and datetime.now(timezone.utc).timestamp() < deadline:
        pass
    start_file.write_text("go")
    outputs = [proc.communicate(timeout=10) for proc in procs]

    for proc, (stdout, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, stderr
    parsed = [json.loads(stdout) for stdout, _ in outputs]
    assert sorted(item["reminded"] for item in parsed) == [False, True]
    contexts = [item["context"] for item in parsed if item["context"] is not None]
    assert len(contexts) == 1
    assert job_record["job_id"] in contexts[0]
    reminder = json.loads(
        reservations.hook_reminder_path(repo, job_record["job_id"]).read_text()
    )
    assert reminder["last_reminded_poll_after"] == job_record["next_poll_after"]
    assert reminder["reminder_count_for_poll_after"] == 1


def test_phase6_missing_reservation_retires_advisory_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder_path = reservations.hook_reminder_path(repo, job_record["job_id"])
    reservations.atomic_write_json(
        reminder_path,
        {
            "schema_version": 1,
            "job_id": job_record["job_id"],
            "attempt_id": job_record["active_attempt_id"],
            "reservation_key": job_record["reservation_key"],
            "last_reminded_poll_after": "2026-05-30T11:00:00Z",
            "last_reminded_at": "2026-05-30T11:05:00Z",
            "reminder_count_for_poll_after": 1,
        },
    )
    (registry / "gpu-a.gpu0" / "metadata.json").unlink()
    (registry / "gpu-a.gpu0").rmdir()
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is None
    assert not reminder_path.exists()


def test_phase6_mismatched_reservation_retires_stale_advisory_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    job_record = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    reminder_path = reservations.hook_reminder_path(repo, job_record["job_id"])
    reservations.atomic_write_json(
        reminder_path,
        {
            "schema_version": 1,
            "job_id": job_record["job_id"],
            "attempt_id": job_record["active_attempt_id"],
            "reservation_key": job_record["reservation_key"],
            "last_reminded_poll_after": "2026-05-30T11:00:00Z",
            "last_reminded_at": "2026-05-30T11:05:00Z",
            "reminder_count_for_poll_after": 1,
        },
    )
    metadata_path = registry / "gpu-a.gpu0" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["attempt_id"] = "attempt-20260530T123456Z-other"
    metadata_path.write_text(json.dumps(metadata))
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.check_gpu_job_reminders(
        repo,
        now=datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
    )

    assert result is None
    assert not reminder_path.exists()


def test_phase6_main_emits_pretooluse_additional_context(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_TEST_NOW", "2026-05-30T12:05:00Z")
    monkeypatch.setenv("GPU_MCP_HOOK_REMINDER_MODE", "additionalContext")
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({"hook_event_name": "PreToolUse", "cwd": str(repo)}),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert "hookSpecificOutput" in result
    assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "additionalContext" in result["hookSpecificOutput"]
    assert "manage_gpu_job" in result["hookSpecificOutput"]["additionalContext"]


def test_phase6_main_suppresses_reminder_for_inflight_targeted_status(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_TEST_NOW", "2026-05-30T12:05:00Z")
    job = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(repo),
                "tool_name": "mcp__repo_managed_gpu__manage_gpu_job",
                "tool_input": {"action": "status", "job_id": job["job_id"]},
            }
        ),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert not reservations.hook_reminder_path(repo, job["job_id"]).exists()


def test_pretool_stale_policy_block_remains_authoritative(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    config.write_text(config.read_text().replace("nodes = ['gpu-a']", "nodes = ['gpu-a', 'gpu-b']"))

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({
            "hook_event_name": "PreToolUse",
            "cwd": str(repo),
            "tool_name": "repo_managed_gpu/run_python_on_gpu",
            "tool_input": {
                "host": "gpu-a",
                "gpu_index": 0,
                "script_path": "jobs/train.py",
                "async_mode": True,
            },
        }),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["decision"] == "block"
    assert "gpu-mcp.toml has changed" in result["reason"]
    assert "hookSpecificOutput" not in result


def _configure_stop_hook_test(tmp_path, monkeypatch) -> tuple[Path, dict[str, str]]:
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))
    monkeypatch.setenv("GPU_MCP_TEST_STOP_STATE_ROOT", str(tmp_path / "stop-state"))
    return registry, dict(os.environ)


def test_stop_hook_due_event_blocks_once_per_turn(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    hook = importlib.import_module("gpu_mcp_policy_hook")
    now = lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)

    first = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=now,
        max_wait_sec=0,
    )
    sleeps: list[float] = []
    real_monotonic = hook.time.monotonic
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(hook.time, "monotonic", lambda: next(ticks))
    duplicate = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=now,
        sleep_fn=sleeps.append,
        max_wait_sec=1,
    )
    monkeypatch.setattr(hook.time, "monotonic", real_monotonic)
    next_turn = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-b",
        env=env,
        now_fn=now,
        max_wait_sec=0,
    )

    assert first is not None and first["decision"] == "block"
    assert job["job_id"] in first["reason"]
    assert 'manage_gpu_job(action="status"' in first["reason"]
    assert duplicate is None
    assert sleeps == []
    assert next_turn is not None and next_turn["decision"] == "block"


def test_stop_hook_outcome_presence_wakes_without_parsing_or_mutating_job(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2099-01-01T00:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    job_path = reservations.job_record_path(repo, job["job_id"])
    metadata_path = registry / job["reservation_key"] / "metadata.json"
    outcome_path = reservations.outcome_record_path(
        repo,
        job["job_id"],
        job["active_attempt_id"],
    )
    outcome_path.parent.mkdir(parents=True)
    outcome_path.write_text("{malformed outcome", encoding="utf-8")
    job_before = job_path.read_bytes()
    metadata_before = metadata_path.read_bytes()
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
        max_wait_sec=0,
    )

    assert result is not None and result["decision"] == "block"
    assert "local outcome" in result["reason"]
    assert "Status reconciles managed state" in result["reason"]
    assert "Wait again only" not in result["reason"]
    assert job_path.read_bytes() == job_before
    assert metadata_path.read_bytes() == metadata_before


def test_stop_hook_waits_until_outcome_appears(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2099-01-01T00:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    outcome_path = reservations.outcome_record_path(
        repo,
        job["job_id"],
        job["active_attempt_id"],
    )
    sleeps: list[float] = []

    def create_outcome_after_first_sleep(delay: float) -> None:
        sleeps.append(delay)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text("{}\n", encoding="utf-8")

    hook = importlib.import_module("gpu_mcp_policy_hook")
    result = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
        sleep_fn=create_outcome_after_first_sleep,
        max_wait_sec=1,
    )

    assert sleeps
    assert result is not None and "local outcome detected" in result["reason"]


def test_stop_hook_allows_new_cadence_event_in_same_continued_turn(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    first = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
        max_wait_sec=0,
    )
    job_path = reservations.job_record_path(repo, job["job_id"])
    updated = json.loads(job_path.read_text())
    updated["last_status_checked_at"] = "2026-05-30T12:05:00Z"
    updated["next_poll_after"] = "2026-05-30T12:10:00Z"
    reservations.atomic_write_json(job_path, updated)
    second = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 10, tzinfo=timezone.utc),
        max_wait_sec=0,
    )

    assert first is not None and first["decision"] == "block"
    assert second is not None and second["decision"] == "block"


def test_stop_hook_requires_matching_active_reservation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    reservations = importlib.import_module("gpu_mcp_reservations")
    metadata_path = registry / job["reservation_key"] / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["attempt_id"] = "attempt-20260530T123456Z-different"
    reservations.atomic_write_json(metadata_path, metadata)
    hook = importlib.import_module("gpu_mcp_policy_hook")

    result = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
        max_wait_sec=0,
    )

    assert result is None


def test_stop_hook_fails_open_when_deduplication_state_cannot_be_written(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo)
    registry, env = _configure_stop_hook_test(tmp_path, monkeypatch)
    _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    hook = importlib.import_module("gpu_mcp_policy_hook")

    def fail_write(*args, **kwargs):
        raise OSError("test state write failure")

    monkeypatch.setattr(hook.reservations, "atomic_write_json", fail_write)
    result = hook.wait_for_gpu_job_stop_event(
        repo,
        session_id="session-a",
        turn_id="turn-a",
        env=env,
        now_fn=lambda: datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc),
        max_wait_sec=0,
    )

    assert result is None


def test_stop_hook_main_emits_codex_block_shape(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _write_config(repo)
    store = tmp_path / "approved-policies.json"
    _approve_policy(config, store)
    registry, _env = _configure_stop_hook_test(tmp_path, monkeypatch)
    job = _seed_hook_job(repo, registry, next_poll_after="2026-05-30T12:00:00Z")
    monkeypatch.setenv("GPU_MCP_TEST_NOW", "2026-05-30T12:05:00Z")

    completed = subprocess.run(
        [sys.executable, str(HOOK), "--store", str(store)],
        input=json.dumps({
            "hook_event_name": "Stop",
            "cwd": str(repo),
            "session_id": "session-a",
            "turn_id": "turn-a",
            "stop_hook_active": False,
        }),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["decision"] == "block"
    assert job["job_id"] in result["reason"]
    assert "hookSpecificOutput" not in result
