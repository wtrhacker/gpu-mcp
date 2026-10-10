"""Completed jobs survive a real server re-import without orphaning their GPU.

Only process-table observations are controlled. Policies, reservation locks,
fresh imports, protected records, public tools and result transactions are real.
All files and reservations belong to pytest; no GPU or SSH work is launched.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import gpu_mcp_reservations as leases
from gpu_mcp_codex_runtime import CodexRuntime
from gpu_mcp_codex_state import Origin
from test_gpu_mcp_codex_shared import SOURCE, QuietDaemon, gpu_job, publish_outcome, shared

pytestmark = pytest.mark.contract


@pytest.fixture
def restarted(shared):
    servers, runtimes = [], []

    def make(mode="shared", *, code=0, publish=True, age=7200, cached=False):
        job = gpu_job(shared)
        if publish:
            publish_outcome(shared, job, code=code)
        if cached:
            outcome = shared.server._read_outcome_record(job)
            with shared.runtime.acting_as(shared.owner):
                shared.server._write_repo_job_record(dict(job, outcome=outcome,
                    job_lifecycle="succeeded", next_poll_after=None))
            leases.outcome_record_path(shared.repo, job["job_id"], job["active_attempt_id"]).unlink()
        shared.runtime.stop()
        shared.server.HEARTBEAT_MANAGER.stop()
        path = leases.reservation_dir(leases.reservation_registry_root(), job["reservation_key"])
        metadata_path = path / "metadata.json"
        metadata = leases.read_json_file_no_follow(metadata_path)
        metadata["last_heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        leases.atomic_write_json(metadata_path, metadata)

        shared.monkeypatch.setattr(sys, "argv", ["gpu_mcp_server.py", "--config", str(shared.repo / "gpu-mcp.toml")])
        name = "restart_recovery_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, SOURCE / "gpu_mcp_server.py")
        server = importlib.util.module_from_spec(spec)
        shared.monkeypatch.setitem(sys.modules, name, server)
        spec.loader.exec_module(server)
        servers.append(server)
        runtime = None
        if mode == "shared":
            runtime = CodexRuntime(server, shared.runtime.store.root, daemon=QuietDaemon())
            runtimes.append(runtime)
            server.CODEX_RUNTIME = runtime
            shared.monkeypatch.setattr(server.mcp, "get_context", lambda: types.SimpleNamespace(
                request_context=types.SimpleNamespace(meta=dict(shared.actor))))
        inspections = []

        def inspect(snapshot):
            inspections.append(dict(snapshot))
            return {"status": "gone", "reason": "controlled original-process exit",
                    "remote_pid": snapshot["remote_pid"]}

        shared.monkeypatch.setattr(server, "_inspect_reservation_process", inspect)
        assert server.SERVER_INSTANCE_ID != job["server_instance_id"]
        return types.SimpleNamespace(server=server, runtime=runtime, job=job, path=path,
            metadata_path=metadata_path, inspections=inspections, source=shared, mode=mode)

    yield make
    for runtime in runtimes:
        runtime.stop()
    for server in servers:
        server.HEARTBEAT_MANAGER.stop()


def status(case):
    return json.loads(case.server.manage_gpu_job(action="status", job_id=case.job["job_id"], detail="full"))


def saved(case):
    return leases.read_json_file_no_follow(leases.job_record_path(case.source.repo, case.job["job_id"]))


def assert_saved_terminal(case, lifecycle, *, held):
    record = saved(case)
    assert record["job_lifecycle"] == lifecycle
    assert record["active_reservation"] is held
    assert record["next_poll_after"] is None
    assert record["outcome"]["attempt_id"] == case.job["active_attempt_id"]
    assert record["server_instance_id"] == case.job["server_instance_id"]
    assert case.server.HEARTBEAT_MANAGER.owned_keys() == []
    if case.runtime:
        assert case.runtime.store.get(case.job["job_id"]) == record


@pytest.mark.parametrize("mode", ["direct", "shared"])
@pytest.mark.parametrize("code,lifecycle", [(0, "succeeded"), (17, "failed")])
def test_status_recovers_completed_orphan_and_persists_it(restarted, mode, code, lifecycle):
    case = restarted(mode, code=code)
    for _ in range(3):
        result = status(case)
        assert result["job_lifecycle"] == lifecycle
        assert result["active_reservation"] is False
        assert result["reservation_state"] is None
        assert result["allowed_actions"] == ["status"]
    assert not case.path.exists()
    assert_saved_terminal(case, lifecycle, held=False)
    quarantined = list((case.path.parent / ".quarantine").glob(case.job["reservation_key"] + ".*"))
    assert len(quarantined) == 1


@pytest.mark.parametrize("mode", ["direct", "shared"])
def test_status_persists_completion_after_separate_stale_cleanup(restarted, mode):
    case = restarted(mode)
    assert json.loads(case.server.list_gpu_reservations(scope="mine", fresh=True))["status"] == "ok"
    assert not case.path.exists()
    assert status(case)["active_reservation"] is False
    assert_saved_terminal(case, "succeeded", held=False)


@pytest.mark.parametrize("mode", ["direct", "shared"])
@pytest.mark.parametrize("observation,age", [("alive", 7200), ("unknown", 7200), ("gone", 0)])
def test_completion_is_saved_without_releasing_unproven_or_fresh_orphan(restarted, mode, observation, age):
    case = restarted(mode, age=age)
    case.source.monkeypatch.setattr(case.server, "_inspect_reservation_process",
        lambda metadata: {"status": observation, "reason": observation, "remote_pid": metadata["remote_pid"]})
    before = leases.read_json_file_no_follow(case.metadata_path)
    result = status(case)
    assert result["job_lifecycle"] == "succeeded"
    assert result["active_reservation"] is True
    assert leases.read_json_file_no_follow(case.metadata_path) == before
    assert_saved_terminal(case, "succeeded", held=True)


@pytest.mark.parametrize("cached", [False, True])
def test_background_recovers_old_completion_and_notifies_only_its_owner(restarted, cached):
    case = restarted(cached=cached)
    case.runtime.monitor_once()
    assert not case.path.exists()
    assert_saved_terminal(case, "succeeded", held=False)
    case.runtime.monitor_once()
    case.runtime.deliver_once()
    completed = [e for e in case.runtime.daemon.sent if e["kind"] == "succeeded"]
    assert len(completed) == 1
    assert completed[0]["origin"] == case.source.owner.thread_id
    assert case.server.SERVER_INSTANCE_ID != saved(case)["server_instance_id"]


def test_background_keeps_old_live_job_then_recovers_its_later_result(restarted):
    case = restarted(publish=False)
    case.runtime.monitor_once()
    assert case.inspections == []
    assert case.runtime.store.get(case.job["job_id"])["job_lifecycle"] == "running"
    assert case.path.exists()
    notices = case.runtime.store.events()
    assert len(notices) == 1 and notices[0]["kind"] == "backend_restarted"
    publish_outcome(case.source, case.job)
    case.runtime.monitor_once()
    assert_saved_terminal(case, "succeeded", held=False)
    case.runtime.deliver_once()
    assert [e["kind"] for e in case.runtime.daemon.sent] == ["succeeded"]


def test_background_saves_result_then_waits_for_staleness_without_remote_polling(restarted):
    case = restarted(age=0)
    for _ in range(3):
        case.runtime.monitor_once()
    assert_saved_terminal(case, "succeeded", held=True)
    assert case.inspections == []
    metadata = leases.read_json_file_no_follow(case.metadata_path)
    metadata["last_heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    leases.atomic_write_json(case.metadata_path, metadata)
    case.runtime.monitor_once()
    assert not case.path.exists()
    assert_saved_terminal(case, "succeeded", held=False)


def test_failed_result_transaction_keeps_orphan_reserved_and_can_retry(restarted):
    case = restarted()
    with sqlite3.connect(case.runtime.store.path) as db:
        db.execute("CREATE TRIGGER reject_terminal BEFORE INSERT ON events WHEN NEW.kind='succeeded' "
                   "BEGIN SELECT RAISE(ABORT, 'result unavailable'); END")
    case.runtime.monitor_once()
    assert case.path.exists()
    assert case.runtime.store.get(case.job["job_id"])["job_lifecycle"] == "running"
    with sqlite3.connect(case.runtime.store.path) as db:
        db.execute("DROP TRIGGER reject_terminal")
    case.runtime.monitor_once()
    assert not case.path.exists()
    assert_saved_terminal(case, "succeeded", held=False)


@pytest.mark.parametrize("field", ["attempt_id", "process_fingerprint", "remote_start_time", "remote_boot_id", "server_instance_id"])
def test_cleanup_rereads_original_attempt_and_process_identity_under_lock(restarted, field):
    case = restarted()
    original = leases.read_json_file_no_follow(case.metadata_path)
    changed = dict(original, **{field: leases.generate_attempt_id() if field == "attempt_id" else "changed-identity"})
    def inspect_and_replace(metadata):
        leases.atomic_write_json(case.metadata_path, changed)
        return {"status": "gone", "reason": "controlled exit with concurrent identity replacement"}
    case.source.monkeypatch.setattr(case.server, "_inspect_reservation_process", inspect_and_replace)
    cleaned, observation = case.server._cleanup_stale_gone_reservation(case.job["reservation_key"], original)
    assert not cleaned
    assert "changed" in observation["reason"]
    assert leases.read_json_file_no_follow(case.metadata_path) == changed


def test_completed_old_job_does_not_remove_a_replacement_booking(restarted):
    case = restarted()
    replacement = leases.read_json_file_no_follow(case.metadata_path)
    replacement.update(job_id=leases.generate_job_id(), attempt_id=leases.generate_attempt_id())
    leases.atomic_write_json(case.metadata_path, replacement)
    case.runtime.monitor_once()
    assert leases.read_json_file_no_follow(case.metadata_path) == replacement
    assert case.inspections == []
    assert_saved_terminal(case, "succeeded", held=False)


def test_recovery_does_not_give_another_conversation_access(restarted):
    case = restarted()
    case.source.actor["threadId"] = str(uuid.uuid4())
    refused = status(case)
    assert refused["status"] == "refused"
    assert case.path.exists()
    assert case.runtime.store.records(case.source.actor["threadId"]) == []
    case.runtime.monitor_once()
    completed = [e for e in case.runtime.store.events() if e["kind"] == "succeeded"]
    assert len(completed) == 1 and completed[0]["origin"] == case.source.owner.thread_id


def test_orphan_recovery_keeps_heartbeat_of_a_new_job_claiming_the_freed_gpu(restarted):
    case = restarted()
    original = case.server._cleanup_stale_gone_reservation
    replacement = {}

    def recover_then_claim(key, metadata):
        result = original(key, metadata)
        assert result[0]
        other = types.SimpleNamespace(server=case.server, runtime=case.runtime, repo=case.source.repo,
                                      owner=Origin(str(uuid.uuid4())))
        replacement.update(gpu_job(other))
        return result

    case.source.monkeypatch.setattr(case.server, "_cleanup_stale_gone_reservation", recover_then_claim)
    assert status(case)["active_reservation"] is False
    assert leases.read_json_file_no_follow(case.metadata_path)["job_id"] == replacement["job_id"]
    assert case.server.HEARTBEAT_MANAGER.owned_keys() == [replacement["reservation_key"]]
    assert case.server.HEARTBEAT_MANAGER.heartbeat_once(replacement["reservation_key"])
    assert case.runtime.store.get(replacement["job_id"])["active_reservation"] is True
