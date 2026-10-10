"""An execution host's verified result survives a delayed control-side file view.

No SSH or GPU work is launched. Process observations and the two filesystem
views are controlled independently; public tools and result transactions are real.
"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import gpu_mcp_reservations as leases
from test_gpu_mcp_codex_shared import call, gpu_job, shared

pytestmark = pytest.mark.contract


@pytest.fixture
def remote_case(shared):
    job = gpu_job(shared)
    job = dict(job, launch_mode="ssh", job_lifecycle="running",
               reason="Process is gone; its verified final result is not available yet.",
               last_process_observation={"status": "alive", "reason": "earlier check"})
    with shared.runtime.acting_as(shared.owner):
        shared.server._write_repo_job_record(job)
    outcome = leases.build_outcome_record(job_id=job["job_id"], attempt_id=job["active_attempt_id"],
        reservation_key_value=job["reservation_key"], host=job["host"], gpu_index=job["gpu_index"],
        remote_pid=job["remote_pid"], terminal_status="success", exit_code=0,
        started_at=job["created_at"], ended_at=leases.iso_timestamp())
    requests = []
    shared.monkeypatch.setattr(shared.server, "_read_outcome_record", lambda record: None)
    shared.monkeypatch.setattr(shared.server, "_inspect_reservation_process",
        lambda metadata: {"status": "gone", "reason": "matching original process exited", "remote_pid": metadata["remote_pid"]})

    def producer(host, command, **kwargs):
        requests.append((host, command, kwargs))
        return json.dumps(outcome)

    shared.monkeypatch.setattr(shared.server, "_host_run", producer)
    return shared, job, outcome, requests


@pytest.mark.parametrize("code,lifecycle", [(0, "succeeded"), (17, "failed")])
def test_producer_result_closes_job_without_an_unknown_alert(remote_case, code, lifecycle):
    f, job, outcome, requests = remote_case
    outcome.update(exit_code=code, terminal_status="success" if code == 0 else "failure")
    f.runtime.monitor_once()
    saved = f.runtime.store.get(job["job_id"])
    assert saved["job_lifecycle"] == lifecycle
    assert saved["active_reservation"] is False
    assert saved["next_poll_after"] is None
    assert saved["reason"] == ""
    assert saved["last_process_observation"]["status"] == "gone"
    assert saved["outcome"] == outcome
    assert len(requests) == 1 and requests[0][0] == job["host"]
    events = f.runtime.store.events(pending=True)
    assert [e["kind"] for e in events] == [lifecycle]
    assert events[0]["payload"]["error_summary"] == ""


@pytest.mark.parametrize("field,value", [
    ("job_id", "job-20261010T000000Z-wrong"),
    ("attempt_id", "attempt-20261010T000000Z-wrong"),
    ("remote_pid", 987654), ("host", "wrong-host"), ("gpu_index", 7),
    ("reservation_key", "fixture.gpu7"), ("schema_version", 99),
    ("terminal_status", "running"),
])
def test_foreign_or_incomplete_producer_result_keeps_reservation(remote_case, field, value):
    f, job, outcome, requests = remote_case
    outcome[field] = value
    result = call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert result["job_lifecycle"] == "process_gone_unknown_outcome"
    assert result["active_reservation"] is True
    assert result["outcome"] is None
    assert len(requests) == 1
    assert f.runtime.store.get(job["job_id"])["last_process_observation"]["status"] == "gone"


@pytest.mark.parametrize("reply", [None, "not JSON", "[]", "{}"])
def test_unavailable_or_malformed_producer_reply_is_not_success(remote_case, reply):
    f, job, _, _ = remote_case
    f.monkeypatch.setattr(f.server, "_host_run", lambda *args, **kwargs: reply)
    result = call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert result["job_lifecycle"] == "process_gone_unknown_outcome"
    assert result["active_reservation"] is True


@pytest.mark.parametrize("observation", ["alive", "unknown"])
def test_live_or_uncertain_process_does_not_trigger_producer_fetch(remote_case, observation):
    f, job, _, requests = remote_case
    f.monkeypatch.setattr(f.server, "_inspect_reservation_process",
        lambda metadata: {"status": observation, "reason": observation})
    call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert requests == []
    assert f.runtime.store.get(job["job_id"])["active_reservation"] is True


def test_local_job_keeps_its_local_verification_path(remote_case):
    f, job, _, requests = remote_case
    with f.runtime.acting_as(f.owner):
        f.server._write_repo_job_record(dict(job, launch_mode="local"))
    result = call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert result["job_lifecycle"] == "process_gone_unknown_outcome"
    assert requests == []


@pytest.mark.parametrize("file_kind", ["regular", "symlink", "oversized"])
def test_generated_read_is_bound_to_the_attempt_and_checks_the_file(remote_case, file_kind):
    f, job, outcome, _ = remote_case
    path = leases.outcome_record_path(f.repo, job["job_id"], job["active_attempt_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if file_kind == "symlink":
        target = f.repo / "results/elsewhere.json"
        leases.atomic_write_json(target, outcome)
        path.symlink_to(target)
    elif file_kind == "oversized":
        path.write_text(json.dumps(outcome) + " " * (128 * 1024))
    else:
        leases.atomic_write_json(path, outcome)
    commands = []

    def actual_read(host, command, **kwargs):
        argv = shlex.split(command)
        assert host == job["host"] and argv[-1] == str(path)
        assert argv[1] == "-c"
        commands.append(argv)
        result = subprocess.run([sys.executable, *argv[1:]], capture_output=True, text=True, timeout=3)
        return result.stdout if result.returncode == 0 else None

    f.monkeypatch.setattr(f.server, "_host_run", actual_read)
    result = call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert len(commands) == 1
    assert result["job_lifecycle"] == ("succeeded" if file_kind == "regular" else "process_gone_unknown_outcome")
    assert result["active_reservation"] is (file_kind != "regular")


def test_foreign_session_cannot_fetch_execution_host_result(remote_case):
    f, job, _, requests = remote_case
    f.actor["threadId"] = "01a126e0-0000-7000-8000-000000000001"
    result = call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    assert result["status"] == "refused"
    assert requests == []


def test_logs_use_latest_gone_observation_when_result_is_missing(remote_case):
    f, job, _, _ = remote_case
    f.monkeypatch.setattr(f.server, "_host_run", lambda *args, **kwargs: None)
    call(f, "manage_gpu_job", action="status", job_id=job["job_id"], detail="full")
    logs = call(f, "manage_gpu_job", action="logs", job_id=job["job_id"])
    assert logs["process"]["status"] == "gone"


def test_producer_read_retries_while_control_view_stays_negative(remote_case):
    f, job, outcome, requests = remote_case
    def delayed_producer(host, command, **kwargs):
        requests.append((host, command, kwargs))
        return None if len(requests) == 1 else json.dumps(outcome)
    f.monkeypatch.setattr(f.server, "_host_run", delayed_producer)
    f.runtime.monitor_once()
    previous = f.runtime.store.get(job["job_id"])
    assert previous["job_lifecycle"] == "process_gone_unknown_outcome"
    f.runtime.store.save(dict(previous, verification_pending_since="2000-01-01T00:00:00Z"), origin=f.owner)
    f.runtime._verification_retry_after.clear()
    f.runtime.monitor_once()
    saved = f.runtime.store.get(job["job_id"])
    assert len(requests) == 2
    assert saved["job_lifecycle"] == "succeeded"
    assert saved["reason"] == ""
    assert saved["active_reservation"] is False
