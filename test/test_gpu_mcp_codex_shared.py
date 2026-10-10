"""Fresh contracts exercising the real server through native caller boundaries.

No earlier Codex probe harness is imported. Remote observations are controlled
facts; ownership, policy, result reconciliation, locks and tools are real code.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import gpu_mcp_builds
import gpu_mcp_reservations as leases
from gpu_mcp_codex_daemon import DaemonClient, DeliveryUncertain
from gpu_mcp_codex_runtime import CodexRuntime
from gpu_mcp_codex_state import Origin, SessionAccessError, runtime_root
from gpu_mcp_policy_hook import check_gpu_job_reminders, probe_stop_wait_ownership

pytestmark = pytest.mark.contract
SOURCE = Path(__file__).resolve().parents[1]


class QuietDaemon:
    def __init__(self):
        self.sent = []
        self.result = "accepted"

    def deliver(self, event, repo, *, before_send):
        before_send()
        self.sent.append(event)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        pass


@pytest.fixture()
def shared(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    for name in ("jobs", "results", "logs"):
        (repo / name).mkdir(parents=True)
    (repo / "jobs/task.py").write_text("print('fixture task')\n")
    config = repo / "gpu-mcp.toml"
    config.write_text(f"schema_version = 1\nrepo_root = {str(repo)!r}\nnodes = ['fixture']\n"
                      "script_roots = ['jobs']\nwrite_roots = ['results']\noutput_roots = ['logs']\n"
                      "min_free_memory_mib = 0\nsync_timeout_sec = 2\nbuild_enabled = true\n")
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "leases"))
    monkeypatch.setenv("GPU_MCP_TEST_BUILD_CONTROL_ROOT", str(tmp_path / "build-control"))
    monkeypatch.setattr(sys, "argv", ["gpu_mcp_server.py", "--config", str(config)])
    name = "codex_shared_contract_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, SOURCE / "gpu_mcp_server.py")
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, server)
    spec.loader.exec_module(server)
    daemon = QuietDaemon()
    runtime = CodexRuntime(server, tmp_path / "authority", daemon=daemon)
    server.CODEX_RUNTIME = runtime
    actor = {"threadId": str(uuid.uuid4())}
    monkeypatch.setattr(server.mcp, "get_context", lambda: types.SimpleNamespace(
        request_context=types.SimpleNamespace(meta=dict(actor))))
    monkeypatch.setattr(server, "_inspect_reservation_process", lambda metadata: {
        "status": "alive", "reason": "controlled matching process", "remote_pid": metadata.get("remote_pid")})
    result = types.SimpleNamespace(server=server, runtime=runtime, daemon=daemon, repo=repo,
                                   actor=actor, owner=Origin(actor["threadId"]), monkeypatch=monkeypatch)
    yield result
    runtime.stop()
    server.HEARTBEAT_MANAGER.stop()


def gpu_job(shared, *, origin=None, gpu=0):
    server, runtime = shared.server, shared.runtime
    origin = origin or shared.owner
    job, attempt = leases.generate_job_id(), leases.generate_attempt_id()
    key = leases.reservation_key("fixture", gpu)
    record = leases.build_job_record(job_id=job, attempt_id=attempt, reservation_key_value=key,
        host="fixture", gpu_index=gpu, script_path=shared.repo / "jobs/task.py", args=[],
        output_file=shared.repo / "logs" / (job + ".log"), server_instance_id=server.SERVER_INSTANCE_ID,
        next_poll_after="2000-01-01T00:00:00Z")
    record.update(job_lifecycle="running", active_reservation=True, remote_pid=8100 + gpu,
                  poll_interval_sec=300, phase7_poll_discipline=True)
    metadata = leases.build_shared_metadata(job_id=job, attempt_id=attempt, reservation_key_value=key,
        host="fixture", gpu_index=gpu, repo=shared.repo, script_path=record["script_path"],
        owner_user=server.GPU_MCP_USER, server_instance_id=server.SERVER_INSTANCE_ID, remote_pid=record["remote_pid"])
    assert leases.acquire_reservation(registry_root=leases.reservation_registry_root(),
                                     reservation_key_value=key, metadata=metadata)[0]
    server.HEARTBEAT_MANAGER.register(key, interval_sec=600)
    with runtime.acting_as(origin):
        record = runtime.register(record)
        server._write_repo_job_record(record)
    Path(record["output_file"]).write_text("private log for " + job + "\n")
    return record


def publish_outcome(shared, record, *, code=0):
    outcome = leases.build_outcome_record(job_id=record["job_id"], attempt_id=record["active_attempt_id"],
        reservation_key_value=record["reservation_key"], host=record["host"], gpu_index=record["gpu_index"],
        remote_pid=record["remote_pid"], terminal_status="success" if code == 0 else "failure",
        started_at=record["created_at"], ended_at=leases.iso_timestamp(), exit_code=code)
    leases.atomic_write_json(leases.outcome_record_path(shared.repo, record["job_id"], record["active_attempt_id"]), outcome)


def call(shared, name, **arguments):
    return json.loads(getattr(shared.server, name)(**arguments))


@pytest.mark.parametrize("action", ["status", "stop", "retry", "finish", "update_cadence", "logs", "notifications", "watch", "retry_notification"])
def test_foreign_known_id_cannot_be_inspected_or_changed(shared, action):
    foreign = gpu_job(shared, origin=Origin(str(uuid.uuid4())))
    result = call(shared, "manage_gpu_job", action=action, job_id=foreign["job_id"], detail="full")
    assert result == {"status": "refused", "reason": "job unavailable to this session"}
    assert shared.runtime.store.get(foreign["job_id"])["last_status_checked_at"] is None
    assert shared.daemon.sent == []


def test_listing_implicit_selection_smoke_and_pid_do_not_cross_sessions(shared):
    own = gpu_job(shared)
    foreign = gpu_job(shared, origin=Origin(str(uuid.uuid4())), gpu=1)
    for name, arguments in [("list_gpu_jobs", {}), ("list_gpu_reservations", {"scope": "all"})]:
        result = call(shared, name, **arguments)
        assert own["job_id"] in json.dumps(result)
        assert foreign["job_id"] not in json.dumps(result)
    assert call(shared, "manage_gpu_job")["job_id"] == own["job_id"]
    assert call(shared, "check_gpu_processes", hosts=["fixture"])["status"] == "refused"
    assert call(shared, "kill_gpu_process", host="fixture", pid=foreign["remote_pid"])["status"] == "refused"
    result = call(shared, "run_python_on_gpu", host="fixture", gpu_index=2, script_path="jobs/task.py", smoke_job_id=foreign["job_id"])
    assert result == {"status": "refused", "reason": "job unavailable to this session"}


def test_missing_identity_and_new_fork_fail_closed_but_resume_retains_owner(shared):
    own = gpu_job(shared)
    original = shared.actor["threadId"]
    shared.actor.clear()
    assert call(shared, "list_gpu_jobs")["status"] == "refused"
    shared.actor.update(threadId=str(uuid.uuid4()), sessionId=original)
    assert call(shared, "list_gpu_jobs")["jobs"] == []
    shared.actor["threadId"] = original
    assert call(shared, "list_gpu_jobs")["jobs"][0]["job_id"] == own["job_id"]


def test_forged_display_record_cannot_transfer_ownership(shared):
    own = gpu_job(shared)
    attacker = str(uuid.uuid4())
    leases.atomic_write_json(leases.job_record_path(shared.repo, own["job_id"]), dict(own, origin_thread_id=attacker))
    shared.actor["threadId"] = attacker
    assert call(shared, "manage_gpu_job", job_id=own["job_id"])["status"] == "refused"


def test_healthy_check_sends_only_a_short_poll_update_and_advances_its_schedule(shared):
    own = gpu_job(shared)
    shared.runtime.monitor_once()
    latest = shared.runtime.store.get(own["job_id"])
    assert latest["last_status_checked_at"] is not None
    assert latest["next_poll_after"] > own["next_poll_after"]
    assert latest["job_lifecycle"] == "running"
    shared.runtime.deliver_once()
    assert [event['kind'] for event in shared.daemon.sent] == ['poll']
    assert shared.daemon.sent[0]['payload']['checked_at'] == latest['last_status_checked_at']
    result = call(shared, "manage_gpu_job", job_id=own["job_id"])
    assert "agent_guidance" not in result and "process" not in result and "heartbeat" not in result
    assert "No new job check" in result["message"]
    assert "process" in call(shared, "manage_gpu_job", job_id=own["job_id"], detail="full")


def test_completion_commit_failure_keeps_lease_and_checking_then_recovers(shared):
    own = gpu_job(shared)
    publish_outcome(shared, own)
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", lambda m: {"status": "gone", "reason": "fixture gone"})
    with sqlite3.connect(shared.runtime.store.path) as db:
        db.execute("CREATE TRIGGER reject_terminal BEFORE INSERT ON events WHEN NEW.kind='succeeded' "
                   "BEGIN SELECT RAISE(ABORT, 'event unavailable'); END")
    shared.runtime.monitor_once()
    assert shared.runtime.store.get(own["job_id"])["job_lifecycle"] == "running"
    assert own["reservation_key"] in shared.server.HEARTBEAT_MANAGER.owned_keys()
    assert (leases.reservation_dir(leases.reservation_registry_root(), own["reservation_key"]) / "metadata.json").exists()
    with sqlite3.connect(shared.runtime.store.path) as db:
        db.execute("DROP TRIGGER reject_terminal")
    shared.runtime.monitor_once()
    latest = shared.runtime.store.get(own["job_id"])
    assert latest["job_lifecycle"] == "succeeded" and latest["next_poll_after"] is None
    assert latest["active_reservation"] is False
    shared.runtime.deliver_once()
    completed = [e for e in shared.daemon.sent if e["kind"] == "succeeded"]
    assert len(completed) == 1 and completed[0]["origin"] == shared.owner.thread_id


@pytest.mark.parametrize("observation", ["alive", "unknown"])
def test_valid_outcome_does_not_release_unproven_process(shared, observation):
    own = gpu_job(shared)
    publish_outcome(shared, own)
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", lambda m: {"status": observation, "reason": observation})
    shared.runtime.monitor_once()
    assert (leases.reservation_dir(leases.reservation_registry_root(), own["reservation_key"]) / "metadata.json").exists()
    assert shared.runtime.store.get(own["job_id"])["active_reservation"]


def test_missing_pid_is_uncertain_and_never_reported_healthy(shared):
    own = gpu_job(shared)
    path = (leases.reservation_dir(leases.reservation_registry_root(), own["reservation_key"]) / "metadata.json")
    metadata = leases.read_json_file_no_follow(path)
    metadata["remote_pid"] = None
    leases.atomic_write_json(path, metadata)
    # Supply precisely the inspection result warranted by missing identity.
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", lambda m: {"status": "unknown", "reason": "missing PID"})
    result = call(shared, "manage_gpu_job", job_id=own["job_id"])
    assert result["job_lifecycle"] == "launch_outcome_unknown"
    assert "running" not in result.get("message", "")
    assert shared.runtime.store.events()[0]["kind"] == "launch_outcome_unknown"


@pytest.mark.parametrize("declared", [60, 600, 3600])
def test_stale_timing_is_ten_minutes_even_with_legacy_intervals(shared, declared):
    now = datetime.now(timezone.utc)
    for seconds, expected in [(599, False), (600, False), (601, True)]:
        record = {"last_heartbeat_at": (now - timedelta(seconds=seconds)).isoformat(), "heartbeat_interval_sec": declared}
        assert shared.server._reservation_age_fields(record, now=now)["heartbeat_stale"] is expected


def test_cpu_build_terminal_event_is_saved_without_reservation(shared):
    def worker_rpc(self, action, spec):
        if action == "launch":
            return {"supervisor": {"pid": 4321}}
        return {"state": "gone", "outcome": {"terminal_status": "success", "exit_code": 0,
                "artifact": {"path": spec["artifact_path"]}}}
    shared.monkeypatch.setattr(gpu_mcp_builds.BuildManager, "rpc", worker_rpc)
    result = call(shared, "build_python_artifact", host="fixture", script_path="jobs/task.py",
                  artifact_path="results/library.so", output_file="logs/build.log", cadence_hint_sec=1,
                  job_title='Compile analysis extension')
    assert result["status"] == "launched"
    job = shared.runtime.store.get(result["job_id"])
    assert job['job_title'] == 'Compile analysis extension'
    assert job["reservation_key"] is None and not job["active_reservation"]
    Path(job["ready_file"]).parent.mkdir(parents=True, exist_ok=True)
    Path(job["ready_file"]).write_text("ready")
    shared.runtime.monitor_once()
    saved = shared.runtime.store.get(result["job_id"])
    assert saved["job_lifecycle"] == "succeeded" and saved["next_poll_after"] is None
    assert any(e["job_id"] == result["job_id"] and e["kind"] == "succeeded" for e in shared.runtime.store.events())
    retried = call(shared, 'manage_gpu_job', action='retry', job_id=result['job_id'])
    assert retried['status'] == 'retried'
    assert shared.runtime.store.get(result['job_id'])['job_title'] == 'Compile analysis extension'


def test_lost_acknowledgement_retains_result_without_repeat_enqueue(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="failed", next_poll_after=None), origin=shared.owner)
    shared.daemon.result = DeliveryUncertain("reply lost")
    shared.runtime.deliver_once()
    event = shared.runtime.store.events()[0]
    assert event["state"] == "uncertain" and event["attempts"] == 1
    assert shared.runtime.store.get(own["job_id"])["job_lifecycle"] == "failed"
    shared.runtime.deliver_once()
    assert len(shared.daemon.sent) == 1
    result = call(shared, "manage_gpu_job", action="notifications", job_id=own["job_id"])
    assert result["events"][0]["state"] == "uncertain"


def test_delivery_holds_job_lock_against_concurrent_retry(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="succeeded", next_poll_after=None), origin=shared.owner)
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()
    def delayed(event, repo, *, before_send):
        before_send()
        entered.set()
        assert release.wait(3)
        return "accepted"
    shared.daemon.deliver = delayed
    delivery = threading.Thread(target=shared.runtime.deliver_once)
    delivery.start()
    assert entered.wait(3)
    def retry():
        shared.runtime.store.save(dict(own, active_attempt_id=leases.generate_attempt_id()),
                                  origin=shared.owner, expected_attempt=own["active_attempt_id"])
        changed.set()
    retry_thread = threading.Thread(target=retry)
    retry_thread.start()
    assert not changed.wait(0.05)
    release.set()
    delivery.join(3)
    retry_thread.join(3)
    assert changed.is_set()


def test_policy_drift_and_lease_failure_are_owner_notices_and_throttled(shared):
    own = gpu_job(shared)
    shared.server.HEARTBEAT_MANAGER.mark_unhealthy_for_tests("fixture heartbeat failure")
    shared.runtime.monitor_once()
    shared.runtime.monitor_once()
    lease_events = [e for e in shared.runtime.store.events() if e["kind"] == "lease_failure"]
    assert len(lease_events) == 1 and lease_events[0]["origin"] == shared.owner.thread_id
    with shared.server.POLICY_CONFIG_PATH.open("a") as config:
        config.write("# human changed policy\n")
    assert "has changed but has not been reloaded" in shared.server.list_gpu_jobs()
    shared.runtime.monitor_once()
    assert any(e["kind"] == "monitor_failure" for e in shared.runtime.store.events())


def test_human_snapshot_is_read_only_and_old_backend_jobs_are_not_adopted(shared):
    own = gpu_job(shared)
    before = shared.runtime.store.get(own["job_id"])
    shared.runtime.snapshot()
    assert shared.runtime.store.get(own["job_id"]) == before
    shared.runtime.store.save(dict(before, server_instance_id=leases.generate_server_instance_id()), origin=shared.owner)
    shared.runtime.monitor_once()
    assert shared.runtime.store.get(own["job_id"])["last_status_checked_at"] is None
    assert any(e["kind"] == "backend_restarted" for e in shared.runtime.store.events())


@pytest.mark.parametrize('failed_write_first', [True, False])
def test_failed_checkin_does_not_alert_a_healthy_other_session(shared, failed_write_first):
    own = gpu_job(shared)
    other = gpu_job(shared, origin=Origin(str(uuid.uuid4())), gpu=1)
    for record in (own, other):
        with shared.runtime.acting_as(Origin(record['origin_thread_id'])):
            shared.runtime.persist(dict(record, next_poll_after='2099-01-01T00:00:00Z'))
    original = shared.server.HEARTBEAT_MANAGER._write_heartbeat
    def write(key, **kwargs):
        if key == own['reservation_key']:
            raise OSError('one reservation could not be updated')
        return original(key, **kwargs)
    shared.monkeypatch.setattr(shared.server.HEARTBEAT_MANAGER, '_write_heartbeat', write)
    order = (own, other) if failed_write_first else (other, own)
    for record in order:
        assert shared.server.HEARTBEAT_MANAGER.heartbeat_once(record['reservation_key']) == (record == other)
    shared.runtime.monitor_once()
    alerts = [e for e in shared.runtime.store.events() if e['kind'] == 'lease_failure']
    assert {e['job_id'] for e in alerts} == {own['job_id']}
    assert {e['origin'] for e in alerts} == {own['origin_thread_id']}
    shared.actor['threadId'] = other['origin_thread_id']
    status = call(shared, 'manage_gpu_job', job_id=other['job_id'], detail='full',
                  early_poll_reason='Verify this lease independently of the failed lease.')
    assert status['heartbeat_manager_healthy'] is True
    assert status['heartbeat_manager_reason'] == ''
    shared.monkeypatch.setattr(shared.server, '_inspect_reservation_process', lambda metadata: {
        'status': 'gone', 'reason': 'controlled original-process exit'})
    # A healthy owner's ordinary cleanup must not depend on another lease's error.
    finished = call(shared, 'manage_gpu_job', action='finish', job_id=other['job_id'])
    assert finished['job_lifecycle'] == 'finished'


def test_quiet_checks_are_human_observations_without_events_or_agent_payloads(shared):
    own = gpu_job(shared)
    with shared.runtime.acting_as(shared.owner):
        shared.runtime.persist(dict(own, next_poll_after='2099-01-01T00:00:00Z'))
    assert shared.server.HEARTBEAT_MANAGER.heartbeat_once(own['reservation_key'])
    before = shared.runtime.store.get(own['job_id'])
    shared.runtime.monitor_once()
    snapshot = shared.runtime.snapshot()
    observation = snapshot['monitoring'][own['job_id']]
    assert observation['last_result_scan_at']
    assert observation['result_file_visible'] is False
    assert observation['lease']['last_success_at']
    assert shared.runtime.store.get(own['job_id']) == before
    assert shared.runtime.store.events() == []
    assert 'monitoring' not in call(shared, 'list_gpu_jobs')


def test_checkin_history_follows_a_retried_attempt_without_rewriting_the_old_one(shared):
    own = gpu_job(shared)
    manager = shared.server.HEARTBEAT_MANAGER
    assert manager.heartbeat_once(own['reservation_key'], now='2026-10-08T21:00:00Z')
    previous = manager.diagnostics(own['job_id'], own['active_attempt_id'])
    metadata_path = leases.reservation_dir(leases.reservation_registry_root(), own['reservation_key']) / 'metadata.json'
    metadata = leases.read_json_file_no_follow(metadata_path)
    attempt = leases.generate_attempt_id()
    leases.atomic_write_json(metadata_path, dict(metadata, attempt_id=attempt))
    assert manager.heartbeat_once(own['reservation_key'], now='2026-10-08T21:01:00Z')
    assert manager.diagnostics(own['job_id'], own['active_attempt_id']) == dict(previous, active=False)
    assert manager.diagnostics(own['job_id'], attempt)['last_success_at'] == '2026-10-08T21:01:00Z'


def test_shared_registration_bypasses_legacy_poll_and_stop_hooks(shared):
    gpu_job(shared)
    codex = shared.repo / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text("[mcp_servers.gpu-cluster-mcp]\nargs = [" +
        repr(str(SOURCE / "gpu_mcp_codex_launcher.py")) + ", '--config', " + repr(str(shared.server.POLICY_CONFIG_PATH)) + "]\n")
    assert check_gpu_job_reminders(shared.repo) is None
    assert probe_stop_wait_ownership(shared.repo)["owns_stop"] is False


def test_backend_identity_uses_canonical_repo_not_policy_filename(tmp_path):
    assert runtime_root(tmp_path / "repo/gpu-mcp.toml") == runtime_root(tmp_path / "repo/alternative.toml")


def test_shutdown_refuses_active_jobs_and_authority_cannot_overlap_job_outputs(shared):
    gpu_job(shared)
    assert shared.runtime.shutdown_if_idle()["status"] == "refused"
    from dataclasses import replace
    policy = replace(shared.server.CONFIG_POLICY, write_roots=(shared.runtime.store.root.parent,))
    with pytest.raises(ValueError, match="overlaps"):
        shared.runtime.validate_policy(policy)


@pytest.mark.parametrize("state", ["alive", "unknown"])
def test_ten_minute_stale_investigation_does_not_release_alive_or_unknown(shared, state):
    own = gpu_job(shared)
    path = leases.reservation_dir(leases.reservation_registry_root(), own["reservation_key"]) / "metadata.json"
    metadata = leases.read_json_file_no_follow(path)
    metadata["last_heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    leases.atomic_write_json(path, metadata)
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", lambda m: {"status": state, "reason": state})
    removed, observation = shared.server._cleanup_stale_gone_reservation(own["reservation_key"], metadata)
    assert not removed and observation["status"] == state and path.exists()


def test_stale_cleanup_rereads_a_heartbeat_refresh_under_shared_lock(shared):
    own = gpu_job(shared)
    path = leases.reservation_dir(leases.reservation_registry_root(), own["reservation_key"]) / "metadata.json"
    metadata = leases.read_json_file_no_follow(path)
    metadata["last_heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    leases.atomic_write_json(path, metadata)
    def inspect_and_refresh(snapshot):
        renewed = dict(snapshot, last_heartbeat_at=leases.iso_timestamp())
        leases.atomic_write_json(path, renewed)
        return {"status": "gone", "reason": "fixture exit happened during renewal"}
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", inspect_and_refresh)
    removed, observation = shared.server._cleanup_stale_gone_reservation(own["reservation_key"], metadata)
    assert not removed and "refreshed" in observation["reason"] and path.exists()


def test_mixed_legacy_reader_keeps_its_old_grace_and_shared_reader_is_fixed(shared):
    now = datetime.now(timezone.utc)
    metadata = {"last_heartbeat_at": (now - timedelta(seconds=601)).isoformat(), "heartbeat_interval_sec": 600}
    assert shared.server._reservation_age_fields(metadata, now=now)["heartbeat_stale"]
    shared.server.CODEX_RUNTIME = None
    assert not shared.server._reservation_age_fields(metadata, now=now)["heartbeat_stale"]
    shared.server.CODEX_RUNTIME = shared.runtime


def test_failure_before_send_is_pending_and_archived_delivery_does_not_wake(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="failed", next_poll_after=None), origin=shared.owner)
    def unavailable(event, repo, *, before_send):
        raise ConnectionError("daemon unavailable before enqueue")
    shared.daemon.deliver = unavailable
    shared.runtime.deliver_once()
    event = shared.runtime.store.events()[0]
    assert event["state"] == "pending" and event["attempts"] == 0
    shared.runtime._delivery_after.clear()
    shared.daemon.deliver = lambda event, repo, before_send: "archived"
    shared.runtime.deliver_once()
    assert shared.runtime.store.events()[0]["state"] == "archived"


def test_malformed_outcome_is_not_reinspected_every_second(shared):
    own = gpu_job(shared)
    path = leases.outcome_record_path(shared.repo, own["job_id"], own["active_attempt_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not an outcome")
    observations = []
    def inspect(metadata):
        observations.append(True)
        return {"status": "alive", "reason": "matching fixture"}
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", inspect)
    shared.runtime.monitor_once()
    shared.runtime.monitor_once()
    assert len(observations) == 1
    assert [event['kind'] for event in shared.runtime.store.events()] == ['poll']


def test_gone_unknown_outcome_waits_quietly_for_new_evidence(shared):
    own = gpu_job(shared)
    observations = []
    def inspect(metadata):
        observations.append(True)
        return {"status": "gone", "reason": "fixture exited without result"}
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", inspect)
    shared.runtime.monitor_once()
    shared.runtime.monitor_once()
    assert len(observations) == 1
    assert shared.runtime.store.get(own["job_id"])["job_lifecycle"] == "process_gone_unknown_outcome"
    publish_outcome(shared, own)
    shared.runtime.monitor_once()
    assert shared.runtime.store.get(own["job_id"])["job_lifecycle"] == "succeeded"


def test_finished_job_does_not_wake_for_a_future_watch(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="succeeded", active_reservation=False,
        watches=[{"key": "expired-test", "at": "2000-01-01T00:00:00Z", "attempt_id": own["active_attempt_id"],
                  "message": "requested while running"}]), origin=shared.owner)
    shared.runtime.monitor_once()
    assert not any(e["kind"] == "milestone" for e in shared.runtime.store.events())


def test_owned_pid_inspection_retains_fingerprint_needed_for_safe_second_call(shared):
    own = gpu_job(shared)
    target = {"pid": str(own["remote_pid"]), "owner": shared.server.GPU_MCP_USER, "ppid": "1",
              "pgid": str(own["remote_pid"]), "start_time": "fixture start", "cmd_hash": "fixture hash",
              "cmd_preview": "python approved-task.py", "gpu_index": "0", "gpu_memory_mib": "16",
              "fingerprint": "gpu-mcp-kill-v1:" + "a" * 64}
    shared.monkeypatch.setattr(shared.server, "_inspect_kill_target", lambda host, pid: target)
    result = call(shared, "kill_gpu_process", host="fixture", pid=own["remote_pid"])
    assert result["fingerprint"] == target["fingerprint"]
    assert result["owner"] == target["owner"] and result["cmd_preview"] == target["cmd_preview"]


def test_lost_postlaunch_record_update_is_reconciled_without_adopting_another_server(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="launching", remote_pid=None,
                                  next_poll_after="2099-01-01T00:00:00Z"), origin=shared.owner)
    shared.runtime.monitor_once()
    saved = shared.runtime.store.get(own["job_id"])
    assert saved["job_lifecycle"] == "running" and saved["remote_pid"] == own["remote_pid"]
    assert saved["server_instance_id"] == shared.server.SERVER_INSTANCE_ID


def test_recovered_inspection_updates_saved_lifecycle_and_resolves_unsent_uncertainty(shared):
    own = gpu_job(shared)
    shared.runtime.store.save(dict(own, job_lifecycle="launch_outcome_unknown", reason="old missing acknowledgement"), origin=shared.owner)
    shared.runtime.monitor_once()
    saved = shared.runtime.store.get(own["job_id"])
    assert saved["job_lifecycle"] == "running" and saved["reason"] == ""
    uncertainty = next(event for event in shared.runtime.store.events() if event['kind'] == 'launch_outcome_unknown')
    assert uncertainty['state'] == 'resolved'
    assert [event['kind'] for event in shared.runtime.store.events(pending=True)] == ['poll']


def test_two_sessions_default_logs_are_distinct_even_in_the_same_second(shared):
    shared.monkeypatch.setattr(shared.server.time, "time", lambda: 1234567890)
    shared.monkeypatch.setattr(gpu_mcp_builds.BuildManager, "rpc", lambda self, action, spec: {"supervisor": {"pid": 4321}})
    first = call(shared, "build_python_artifact", host="fixture", script_path="jobs/task.py", artifact_path="results/a.bin")
    shared.actor["threadId"] = str(uuid.uuid4())
    second = call(shared, "build_python_artifact", host="fixture", script_path="jobs/task.py", artifact_path="results/b.bin")
    assert first["output_file"] != second["output_file"]
    assert first["message"] == "Job launched. Backend checks and result delivery are automatic."
    assert second["message"] == first["message"]


@pytest.mark.parametrize("historical", [False, True])
def test_pid_reuse_cannot_grant_inspection_from_an_old_owned_pid(shared, historical):
    own = gpu_job(shared)
    if historical:
        shared.runtime.store.save(dict(own, active_reservation=False, job_lifecycle="succeeded"), origin=shared.owner)
    else:
        shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process", lambda metadata: {
            "status": "gone", "reason": "original start time no longer matches"})
    shared.monkeypatch.setattr(shared.server, "_inspect_kill_target", lambda *args: pytest.fail("foreign replacement process was inspected"))
    result = call(shared, "kill_gpu_process", host="fixture", pid=own["remote_pid"])
    assert result == {"status": "refused", "reason": "process unavailable to this session"}
