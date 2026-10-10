"""Private polling and transient recovery through real launch metadata."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import gpu_mcp_codex_runtime as runtime_module
import gpu_mcp_reservations as reservations
from test_gpu_mcp_codex_uncertainty import launched_server, wait_until

pytestmark = pytest.mark.contract


class Delivery:
    def __init__(self):
        self.active = False
        self.sent = []

    def deliver(self, event, repo, *, before_send):
        if event['kind'] == 'poll' and not self.active:
            return 'pending'
        before_send()
        self.sent.append(event)
        return 'accepted'

    def close(self):
        pass


@pytest.fixture
def monitored(launched_server):
    f = launched_server
    clock = {'seconds': 0.0, 'base': datetime.now(timezone.utc)}

    class Date(datetime):
        @classmethod
        def now(cls, tz=None):
            return (clock['base'] + timedelta(seconds=clock['seconds'])).astimezone(tz or timezone.utc)

    f.monkeypatch.setattr(f.server, 'datetime', Date)
    f.monkeypatch.setattr(runtime_module, 'time', SimpleNamespace(
        monotonic=lambda: clock['seconds'], time=lambda: Date.now().timestamp()))
    f.monkeypatch.setattr(reservations, 'iso_timestamp', lambda: Date.now().strftime('%Y-%m-%dT%H:%M:%SZ'))
    f.runtime.daemon = Delivery()
    f.repo.joinpath('jobs/working.py').write_text("import time\ntime.sleep(30)\nprint('done')\n")
    actual_inspect = f.server._inspect_reservation_process
    jobs = []

    def start(gpu=0):
        result = json.loads(f.server.run_python_on_gpu(host='localhost', gpu_index=gpu,
            script_path='jobs/working.py', job_role='one_off', cadence_hint_sec=2, job_title='Private polling probe'))
        assert result['status'] == 'launched', result
        record = f.runtime.store.get(result['job_id'])
        jobs.append(record)
        return record

    def advance(seconds):
        clock['seconds'] += seconds

    yield SimpleNamespace(**vars(f), start=start, advance=advance, clock=clock, actual_inspect=actual_inspect)
    # These are this fixture's local supervised processes. Restore real identity
    # checks before stopping them through their owning MCP instance.
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', actual_inspect)
    for record in jobs:
        f.server.manage_gpu_job(action='stop', job_id=record['job_id'])
        wait_until(lambda: f.server._read_outcome_record(record))
        f.runtime.monitor_once()


def polls(f):
    return [event for event in f.runtime.store.events() if event['kind'] == 'poll']


def test_background_check_is_a_short_owner_update_and_keeps_inspection_available(monitored):
    f = monitored
    record = f.start()
    f.advance(2)
    f.runtime.monitor_once()
    updates = polls(f)
    assert len(updates) == 1
    packet = updates[0]
    assert packet['origin'] == f.owner.thread_id
    assert packet['payload']['job_id'] == record['job_id']
    assert packet['payload']['checked_at'] == f.runtime.store.get(record['job_id'])['last_status_checked_at']
    assert len(json.dumps(packet['payload'])) < 800
    assert 'heartbeat' not in packet['payload'] and 'agent_guidance' not in packet['payload']
    f.runtime.deliver_once()
    assert f.runtime.daemon.sent == []  # Idle owner need not acknowledge routine health.
    result = json.loads(f.server.manage_gpu_job(job_id=record['job_id'], detail='full',
        early_poll_reason='Inspect the current process for a specific question.'))
    assert result['job_lifecycle'] == 'running' and result['remote_inspection_performed']


def test_idle_checks_coalesce_and_active_owner_receives_only_latest_update(monitored):
    f = monitored
    record = f.start()
    for _ in range(3):
        f.advance(2)
        f.runtime.monitor_once()
        f.runtime.deliver_once()
    pending = [e for e in polls(f) if e['state'] == 'pending']
    assert len(pending) == 1
    assert pending[0]['payload']['checked_at'] == f.runtime.store.get(record['job_id'])['last_status_checked_at']
    assert f.runtime.daemon.sent == []
    f.runtime.daemon.active = True
    f.advance(5)
    f.runtime.deliver_once()
    assert [e['kind'] for e in f.runtime.daemon.sent] == ['poll']
    f.runtime.deliver_once()
    assert len(f.runtime.daemon.sent) == 1


def test_recovery_within_grace_does_not_send_an_uncertainty_alert(monitored):
    f = monitored
    record = f.start()
    failing = {'value': True}
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata:
        {'status': 'unknown', 'reason': 'temporary verification transport error'} if failing['value'] else f.actual_inspect(metadata))
    f.advance(2)
    f.runtime.monitor_once()
    assert f.runtime.store.get(record['job_id'])['job_lifecycle'] == 'launch_outcome_unknown'
    f.runtime.deliver_once()
    assert f.runtime.daemon.sent == []
    failing['value'] = False
    f.advance(2)
    f.runtime.monitor_once()
    assert f.runtime.store.get(record['job_id'])['job_lifecycle'] == 'running'
    f.runtime.deliver_once()
    assert not any(e['kind'] == 'launch_outcome_unknown' for e in f.runtime.daemon.sent)


def test_persistent_uncertainty_alerts_after_grace_once_and_keeps_booking(monitored):
    f = monitored
    record = f.start()
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata:
        {'status': 'unknown', 'reason': 'verification still unavailable'})
    f.advance(2)
    f.runtime.monitor_once()
    f.advance(9)
    f.runtime.monitor_once()
    f.runtime.deliver_once()
    assert f.runtime.daemon.sent == []
    f.advance(1)
    f.runtime.deliver_once()
    assert [e['kind'] for e in f.runtime.daemon.sent] == ['launch_outcome_unknown']
    f.advance(30)
    f.runtime.monitor_once()
    f.runtime.deliver_once()
    assert len(f.runtime.daemon.sent) == 1
    assert (reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json').exists()


def test_retrying_one_job_does_not_pause_another_jobs_checks(monitored):
    f = monitored
    first, second = f.start(), f.start(1)
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata:
        {'status': 'unknown', 'reason': 'one job verification is delayed'}
        if metadata.get('job_id') == first['job_id'] else f.actual_inspect(metadata))
    f.advance(2)
    f.runtime.monitor_once()
    assert f.runtime.store.get(first['job_id'])['job_lifecycle'] == 'launch_outcome_unknown'
    assert f.runtime.store.get(second['job_id'])['job_lifecycle'] == 'running'
    assert [e['job_id'] for e in polls(f)] == [second['job_id']]


def test_completion_supersedes_pending_poll_and_is_not_delayed_by_grace(monitored):
    f = monitored
    record = f.start()
    f.advance(2)
    f.runtime.monitor_once()
    assert any(e['state'] == 'pending' for e in polls(f))
    f.server.manage_gpu_job(action='stop', job_id=record['job_id'])
    wait_until(lambda: f.server._read_outcome_record(record))
    f.runtime.monitor_once()
    f.runtime.deliver_once()
    assert not any(e['state'] in {'pending', 'archived'} for e in polls(f))
    assert [e['kind'] for e in f.runtime.daemon.sent] == ['failed']


def test_a_new_uncertainty_episode_has_its_own_grace_and_notice(monitored):
    f = monitored
    f.start()
    failing = {'value': True}
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata:
        {'status': 'unknown', 'reason': 'temporary identity error'} if failing['value'] else f.actual_inspect(metadata))
    f.advance(2)
    f.runtime.monitor_once()
    f.advance(10)
    f.runtime.deliver_once()
    assert len(f.runtime.daemon.sent) == 1
    failing['value'] = False
    f.advance(2)
    f.runtime.monitor_once()
    failing['value'] = True
    f.advance(2)
    f.runtime.monitor_once()
    f.runtime.deliver_once()
    assert len(f.runtime.daemon.sent) == 1
    f.advance(10)
    f.runtime.deliver_once()
    assert len(f.runtime.daemon.sent) == 2
    assert f.runtime.daemon.sent[0]['event_id'] != f.runtime.daemon.sent[1]['event_id']


def test_static_tool_rules_explain_background_checks_without_repeating_them_in_returns(monitored):
    f = monitored
    record = f.start()
    description = f.server.mcp._tool_manager.get_tool('run_python_on_gpu').description
    assert 'next backend check' in description and 'concrete question or decision' in description
    assert 'acknowledgement tool call' in description
    reply = json.loads(f.server.manage_gpu_job(job_id=record['job_id']))
    assert runtime_module.MONITORING_GUIDANCE not in json.dumps(reply)
