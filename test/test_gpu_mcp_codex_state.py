"""Independent contracts for session identity and crash-safe result publication."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid

import pytest

from gpu_mcp_codex_state import JobStore, Origin, SessionAccessError, StaleAttemptError
import gpu_mcp_reservations as reservations

pytestmark = pytest.mark.contract


def new_record(*, cpu=False):
    return {"job_id": reservations.generate_job_id(),
            "active_attempt_id": reservations.generate_attempt_id(),
            "server_instance_id": reservations.generate_server_instance_id(),
            "job_kind": "build" if cpu else "gpu", "job_lifecycle": "running",
            "active_reservation": not cpu, "reservation_key": None if cpu else "node.gpu0",
            "output_file": "/approved/output.log", "next_poll_after": "2099-01-01T00:00:00Z"}


@pytest.fixture()
def store(tmp_path):
    return JobStore(tmp_path / "authority")


@pytest.fixture()
def owner():
    return Origin(str(uuid.uuid4()))


@pytest.mark.parametrize("meta", [None, {}, {"threadId": "../../escape"}, {"threadId": 42}])
def test_unscoped_or_malformed_callers_are_rejected(meta):
    with pytest.raises(SessionAccessError):
        Origin.from_meta(meta)


def test_thread_identity_is_not_replaced_by_parent_session_or_window():
    thread, parent = str(uuid.uuid4()), str(uuid.uuid4())
    result = Origin.from_meta({"threadId": thread, "sessionId": parent, "windowId": parent + ":2"})
    assert result.thread_id == thread
    assert result.session_id == parent


def test_conflicting_native_thread_fields_fail_closed():
    with pytest.raises(SessionAccessError):
        Origin.from_meta({"threadId": str(uuid.uuid4()),
                          "x-codex-turn-metadata": {"thread_id": str(uuid.uuid4())}})


def test_job_id_knowledge_and_reregistration_do_not_grant_access(store, owner):
    record = new_record()
    other = Origin(str(uuid.uuid4()))
    store.save(record, origin=owner, register=True)
    assert store.get(record["job_id"], other.thread_id) is None
    assert store.records(other.thread_id) == []
    forged = dict(record, origin_thread_id=other.thread_id)
    with pytest.raises(SessionAccessError):
        store.save(forged, origin=other, register=True)
    assert store.get(record["job_id"], owner.thread_id)["origin_thread_id"] == owner.thread_id


@pytest.mark.parametrize("cpu", [False, True])
def test_result_and_event_share_one_transaction_even_without_gpu(store, owner, cpu):
    record = new_record(cpu=cpu)
    store.save(record, origin=owner, register=True)
    done = dict(record, job_lifecycle="succeeded", next_poll_after=None,
                outcome={"terminal_status": "success", "exit_code": 0})
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER prevent_event BEFORE INSERT ON events "
                   "BEGIN SELECT RAISE(ABORT, 'injected event-write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.save(done, origin=owner)
    assert store.get(record["job_id"])["job_lifecycle"] == "running"
    assert store.get(record["job_id"])["next_poll_after"] is not None
    assert store.events() == []
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER prevent_event")
    store.save(done, origin=owner)
    restarted = JobStore(store.root)
    assert restarted.get(record["job_id"])["next_poll_after"] is None
    events = restarted.events(pending=True, origin=owner.thread_id)
    assert len(events) == 1
    assert events[0]["payload"]["job_lifecycle"] == "succeeded"
    assert events[0]["origin"] == owner.thread_id


def test_unknown_outcome_is_persisted_as_uncertainty_not_success(store, owner):
    record = new_record(cpu=True)
    store.save(record, origin=owner, register=True)
    store.save(dict(record, job_lifecycle="process_gone_unknown_outcome", next_poll_after=None), origin=owner)
    event = store.events(pending=True)[0]
    assert event["kind"] == "process_gone_unknown_outcome"
    assert event["payload"]["exit_code"] is None


def test_repeated_observation_is_one_event_and_receipt_survives_restart(store, owner):
    record = new_record()
    store.save(record, origin=owner, register=True)
    failed = dict(record, job_lifecycle="failed", next_poll_after=None,
                  outcome={"terminal_status": "failure", "exit_code": 9})
    for _ in range(5):
        store.save(failed, origin=owner)
    event = store.events(pending=True)[0]
    assert len(store.events()) == 1
    store.delivery(event["event_id"], "accepted")
    restarted = JobStore(store.root)
    assert restarted.events(pending=True) == []
    assert restarted.events()[0]["payload"]["exit_code"] == 9


def test_old_attempt_cannot_overwrite_retry_or_publish_its_result(store, owner):
    record = new_record()
    store.save(record, origin=owner, register=True)
    old = record["active_attempt_id"]
    retried = dict(record, active_attempt_id=reservations.generate_attempt_id())
    store.save(retried, origin=owner, expected_attempt=old)
    with pytest.raises(StaleAttemptError):
        store.save(dict(record, job_lifecycle="failed", next_poll_after=None),
                   origin=owner, expected_attempt=old)
    assert store.get(record["job_id"])["active_attempt_id"] == retried["active_attempt_id"]
    assert store.events() == []


def test_unconditional_attempt_change_is_rejected(store, owner):
    record = new_record()
    store.save(record, origin=owner, register=True)
    with pytest.raises(StaleAttemptError):
        store.save(dict(record, active_attempt_id=reservations.generate_attempt_id()), origin=owner)


def test_resolution_and_supersession_are_not_reported_as_codex_acceptance(store, owner):
    record = new_record()
    store.save(record, origin=owner, register=True)
    store.save(dict(record, job_lifecycle="launch_outcome_unknown"), origin=owner)
    store.save(record, origin=owner)
    assert store.events()[0]["state"] == "resolved"
    assert store.events(pending=True) == []
    store.save(dict(record, job_lifecycle="failed"), origin=owner)
    newer = dict(record, active_attempt_id=reservations.generate_attempt_id())
    store.save(newer, origin=owner, expected_attempt=record["active_attempt_id"])
    store.clear_old_events(record["job_id"], newer["active_attempt_id"])
    assert all(e["state"] == "superseded" for e in store.events())
    assert store.events(pending=True) == []


def test_offline_infrastructure_notices_coalesce_without_erasing_milestones(store, owner):
    record = store.save(new_record(), origin=owner, register=True)
    first = store.notice(record, "lease_failure", "write failed", key="first window")
    second = store.notice(record, "lease_failure", "write still failed", key="next window")
    assert store.event(first)["state"] == "superseded"
    assert store.event(second)["payload"]["event_kind"] == "lease_failure"
    assert [e["event_id"] for e in store.events(pending=True)] == [second]
    store.notice(record, "milestone", "first milestone", key="one")
    store.notice(record, "milestone", "second milestone", key="two")
    assert len(store.events(pending=True)) == 3


def test_locks_are_reentrant_per_job_and_do_not_block_another_job(store, owner):
    first, second = new_record(), new_record(cpu=True)
    store.save(first, origin=owner, register=True)
    store.save(second, origin=owner, register=True)
    entered, waiting, other_done = threading.Event(), threading.Event(), threading.Event()

    def same_job():
        waiting.set()
        with store.lock(first["job_id"]):
            entered.set()

    def different_job():
        with store.lock(second["job_id"]):
            other_done.set()

    with store.lock(first["job_id"]):
        with store.lock(first["job_id"]):
            threads = [threading.Thread(target=same_job), threading.Thread(target=different_job)]
            for thread in threads:
                thread.start()
            assert waiting.wait(2)
            assert other_done.wait(2)
            assert not entered.is_set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert entered.is_set()


def test_authority_database_is_not_a_repository_mirror(store, owner, tmp_path):
    record = new_record()
    store.save(record, origin=owner, register=True)
    mirror = tmp_path / "repo/.gpu_mcp_state/jobs" / record["job_id"] / "job.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps(dict(record, origin_thread_id=str(uuid.uuid4()), job_lifecycle="succeeded")))
    assert store.get(record["job_id"])["job_lifecycle"] == "running"
    assert store.events() == []
    assert store.path.stat().st_mode & 0o077 == 0
