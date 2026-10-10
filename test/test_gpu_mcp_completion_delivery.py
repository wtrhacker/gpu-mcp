"""Exercise delivery while a finished job's result is temporarily unavailable.

Unlike recovery-only tests, these run delivery before another monitor pass.
No network access, Codex turns or GPU computation are involved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import gpu_mcp_reservations as leases
from gpu_mcp_codex_state import UNCERTAIN_STATES
from test_gpu_mcp_codex_shared import gpu_job, shared

pytestmark = pytest.mark.contract


@pytest.fixture
def waiting_result(shared):
    f = shared
    job = gpu_job(f)
    with f.runtime.acting_as(f.owner):
        f.server._write_repo_job_record(dict(job, launch_mode="ssh"))
    producer = {"reply": None, "reads": 0}
    f.monkeypatch.setattr(f.server, "_read_outcome_record", lambda record: None)
    f.monkeypatch.setattr(f.server, "_inspect_reservation_process",
                         lambda metadata: {"status": "gone", "reason": "original process exited"})

    def read_producer(host, command, **kwargs):
        producer["reads"] += 1
        return producer["reply"]

    f.monkeypatch.setattr(f.server, "_host_run", read_producer)
    f.runtime.monitor_once()
    assert f.runtime.store.get(job["job_id"])["job_lifecycle"] == "process_gone_unknown_outcome"
    return f, job, producer


def advance_clock(f, seconds):
    now = datetime.now(timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (now + timedelta(seconds=seconds)).astimezone(tz or timezone.utc)

    f.monkeypatch.setattr(f.server, "datetime", Clock)


@pytest.mark.parametrize("code", [0, 17])
def test_result_available_at_delivery_cancels_the_uncertainty_alert(waiting_result, code):
    f, job, producer = waiting_result
    advance_clock(f, 15)
    producer["reply"] = json.dumps(leases.build_outcome_record(
        job_id=job["job_id"], attempt_id=job["active_attempt_id"],
        reservation_key_value=job["reservation_key"], host=job["host"], gpu_index=job["gpu_index"],
        remote_pid=job["remote_pid"], terminal_status="success" if code == 0 else "failure",
        exit_code=code, started_at=job["created_at"], ended_at=leases.iso_timestamp()))
    # The monitor has not retried. Delivery must verify its old observation.
    f.runtime.deliver_once()
    f.runtime.deliver_once()
    expected = "succeeded" if code == 0 else "failed"
    assert [event["kind"] for event in f.daemon.sent] == [expected]
    saved = f.runtime.store.get(job["job_id"])
    assert saved["job_lifecycle"] == expected
    assert saved["active_reservation"] is False
    assert saved["reason"] == ""
    assert producer["reads"] == 2
    assert all(event["state"] == "resolved" for event in f.runtime.store.events()
               if event["kind"] in UNCERTAIN_STATES)


def test_persistently_missing_result_alerts_once_and_keeps_the_reservation(waiting_result):
    f, job, producer = waiting_result
    advance_clock(f, 15)
    f.runtime.deliver_once()
    f.runtime.deliver_once()
    assert [event["kind"] for event in f.daemon.sent] == ["process_gone_unknown_outcome"]
    assert producer["reads"] == 2
    assert f.runtime.store.get(job["job_id"])["active_reservation"] is True
    assert (leases.reservation_dir(leases.reservation_registry_root(), job["reservation_key"])
            / "metadata.json").is_file()


def test_delivery_during_grace_does_not_alert_or_repeat_the_remote_read(waiting_result):
    f, _, producer = waiting_result
    advance_clock(f, 5)
    f.runtime.deliver_once()
    assert f.daemon.sent == []
    assert producer["reads"] == 1


def test_new_process_observation_replaces_the_previous_uncertainty(shared):
    f = shared
    job = gpu_job(f)
    with f.runtime.acting_as(f.owner):
        f.server._write_repo_job_record(dict(job, launch_mode="ssh"))
    f.monkeypatch.setattr(f.server, "_read_outcome_record", lambda record: None)
    f.monkeypatch.setattr(f.server, "_host_run", lambda *args, **kwargs: None)
    f.monkeypatch.setattr(f.server, "_inspect_reservation_process",
                         lambda metadata: {"status": "unknown", "reason": "temporary transport failure"})
    f.runtime.monitor_once()
    assert f.runtime.store.get(job["job_id"])["job_lifecycle"] == "launch_outcome_unknown"
    f.monkeypatch.setattr(f.server, "_inspect_reservation_process",
                         lambda metadata: {"status": "gone", "reason": "original process exited"})
    advance_clock(f, 15)
    f.runtime._verification_retry_after.clear()
    f.runtime.monitor_once()
    f.runtime.deliver_once()
    assert [event["kind"] for event in f.daemon.sent] == ["process_gone_unknown_outcome"]
    old = [event for event in f.runtime.store.events() if event["kind"] == "launch_outcome_unknown"]
    assert len(old) == 1 and old[0]["state"] == "resolved"
