"""Real guarded case processes with an explicitly fake MPS driver, never CUDA."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import signal
import sqlite3
import fcntl
import threading
import uuid
import subprocess
import shlex
import errno

import pytest

import gpu_mcp_mps_worker as worker
import gpu_mcp_reservations as leases
from test_gpu_mcp_codex_shared import call, shared

pytestmark = pytest.mark.contract


@pytest.fixture
def mps(shared, tmp_path):
    f = shared
    f.monkeypatch.setenv('GPU_MCP_TEST_MPS_DRIVER', 'cpu')
    f.monkeypatch.setenv('GPU_MCP_TEST_MPS_CONTROL_ROOT', str(tmp_path / 'mps-control'))
    f.monkeypatch.setattr(f.server, '_is_local_host', lambda host: True)
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata: {
        'status': f.server._mps_manager().rpc('inspect_reference', metadata['mps_ref'])['state'], 'reason': 'whole MPS group'})
    yield f
    # Test cleanup uses the same safe parent control; report any stuck process.
    for record in f.runtime.store.records():
        if record.get('job_kind') != 'mps':
            continue
        try:
            spec = record['mps_spec']
            path = worker.directory(spec)
            (path / 'test-refuse-termination').unlink(missing_ok=True)
            observed = f.server._mps_manager().rpc('status', spec)
            if observed['state'] != 'gone':
                f.server._mps_manager().rpc('stop', spec, request={'request_id': uuid.uuid4().hex})
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if f.server._mps_manager().rpc('status', spec)['state'] == 'gone':
                    break
                time.sleep(.05)
            assert f.server._mps_manager().rpc('status', spec)['state'] == 'gone', 'fixture leaked an MPS group'
        except FileNotFoundError:
            assert not record.get('active_reservation'), 'unacknowledged launch needs inspection'


def entry(f, label, *, delay=.2, code=0, source=None):
    path = f.repo / 'jobs' / (label + '.py')
    path.write_text(source or f'import time\ntime.sleep({delay})\nprint({label!r})\nraise SystemExit({code})\n')
    return {'case_id': label, 'script_path': 'jobs/' + path.name, 'args': [], 'output_file': 'logs/' + label + '.log'}


def launch(f, entries, **kwargs):
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=entries, cadence_hint_sec=kwargs.pop('cadence_hint_sec', 1),
                  job_role=kwargs.pop('job_role', 'one_off'), **kwargs)
    assert result['status'] == 'launched', result
    return result['job_id']


def status(f, job):
    return call(f, 'manage_gpu_job', job_id=job, action='status',
                early_poll_reason='assert a concrete MPS test condition', detail='full')


def until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('MPS test condition did not become true')


def cases(f, job):
    status(f, job)
    return {c['case_id']: c for c in f.runtime.store.get(job)['cases']}


def test_one_failure_does_not_stop_siblings_or_retry_itself(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=1.2), entry(f, 'later')], max_concurrent=2)
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    saved = f.runtime.store.get(job)
    assert saved['job_lifecycle'] == 'running' and saved['active_reservation']
    assert cases(f, job)['long']['state'] == 'running'
    until(lambda: status(f, job)['job_lifecycle'] == 'failed' and not f.runtime.store.get(job)['active_reservation'])
    final = cases(f, job)
    assert final['long']['state'] == final['later']['state'] == 'succeeded'
    assert len(final['bad']['attempts']) == 1
    assert len([e for e in f.runtime.store.events() if e['kind'] == 'case_failed.bad']) == 1


@pytest.mark.parametrize('local_view', ['missing', 'empty'])
def test_case_logs_read_at_source_when_control_mount_has_not_caught_up(mps, monkeypatch, local_view):
    import gpu_mcp_codex_host as host
    f = mps
    job = launch(f, [entry(f, 'diagnostic', source='print("specific case failure evidence")\nraise SystemExit(17)\n')])
    until(lambda: status(f, job)['job_lifecycle'] == 'failed')
    before = f.runtime.store.get(job)
    events = f.runtime.store.events()
    def local_read(path):
        if local_view == 'missing':
            raise FileNotFoundError(errno.ENOENT, 'simulated delayed NFS visibility', str(path))
        return ''
    monkeypatch.setattr(host, 'read_human_log_tail', local_read)
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    # A separate execution-host Python reads the real file. Only the control
    # mount is made unavailable; no precomputed log reply bypasses the RPC.
    monkeypatch.setattr(f.server, '_host_run', lambda host, command, **kw:
                        subprocess.check_output(shlex.split(command), text=True, timeout=10))
    result = call(f, 'manage_gpu_job', job_id=job, case_id='diagnostic', action='logs')
    assert result['status'] == 'ok', result
    assert 'specific case failure evidence' in result['log_tail']
    assert f.runtime.store.get(job) == before and f.runtime.store.events() == events
    f.actor['threadId'] = str(uuid.uuid4())
    assert call(f, 'manage_gpu_job', job_id=job, case_id='diagnostic', action='logs')['status'] == 'refused'


def test_execution_host_log_reader_is_bounded_and_rejects_symlinks(tmp_path):
    log = tmp_path / 'large.log'
    log.write_text('line\n' * 10000 + 'last\n')
    assert worker.read_log_tail(log, 2) == 'line\nlast\n'
    link = tmp_path / 'link.log'
    link.symlink_to(log)
    with pytest.raises(OSError):
        worker.read_log_tail(link)
    fifo = tmp_path / 'pipe'
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match='regular file'):
        worker.read_log_tail(fifo)


def test_explicit_retry_changes_only_one_attempt_and_keeps_prior_log(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=2)], max_concurrent=2)
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    before = cases(f, job)['bad']
    (f.repo / 'jobs/bad.py').write_text('print("fixed")\n')
    result = call(f, 'manage_gpu_job', job_id=job, case_id='bad', action='retry')
    assert result['status'] == 'retry_queued', result
    until(lambda: cases(f, job)['bad']['state'] == 'succeeded')
    after = cases(f, job)['bad']
    assert after['attempt_id'] != before['attempt_id']
    assert after['output_file'] != before['output_file']
    assert Path(before['output_file']).exists()
    assert len(after['attempts']) == 2
    until(lambda: status(f, job)['job_lifecycle'] == 'succeeded')


def test_targeted_stop_clears_gpu_before_process_and_preserves_sibling(mps):
    f = mps
    job = launch(f, [entry(f, 'stop_me', delay=2), entry(f, 'keep_me', delay=1)], max_concurrent=2)
    until(lambda: len(cases(f, job)) == 2 and all(c['state'] == 'running' for c in cases(f, job).values()))
    result = call(f, 'manage_gpu_job', job_id=job, case_id='stop_me', action='stop')
    assert result['status'] == 'stop_queued', result
    until(lambda: cases(f, job)['stop_me']['state'] == 'cancelled')
    record = f.runtime.store.get(job)
    observed = f.server._mps_manager().rpc('status', record['mps_spec'])
    assert any(r['case_id'] == 'stop_me' for r in observed['termination_receipts'])
    assert record['active_reservation']
    until(lambda: cases(f, job)['keep_me']['state'] == 'succeeded')


def test_unsafe_script_is_rejected_before_gpu_booking(mps):
    f = mps
    bad = entry(f, 'bad', source='import subprocess\nsubprocess.run(["echo","unsafe"])\n')
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True, runs=[bad])
    assert result['status'] == 'refused'
    assert f.runtime.store.records() == []
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()


@pytest.mark.parametrize('field,value', [('max_concurrent',0), ('max_concurrent',True), ('mps_protocol',9),
    ('script_path','jobs/task.py'), ('cadence_hint_sec',10**100)])
def test_invalid_or_mixed_sweep_input_does_not_book(mps, field, value):
    f = mps
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=[entry(f, 'a')], **{field:value})
    assert result['status'] == 'refused', result
    assert f.runtime.store.records() == []


def test_foreign_session_cannot_inspect_or_control_a_case(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=.5)])
    f.actor['threadId'] = str(uuid.uuid4())
    for action in ('status','logs','stop','retry'):
        result = call(f, 'manage_gpu_job', job_id=job, case_id='a', action=action)
        assert result == {'status':'refused','reason':'job unavailable to this session'}


def test_queue_never_exceeds_its_declared_limit(mps):
    f = mps
    job = launch(f, [entry(f, f'c{i}', delay=.12) for i in range(8)], max_concurrent=2)
    observed = []
    def completed():
        data = cases(f, job)
        observed.append(sum(c['state'] in {'running', 'launching'} for c in data.values()))
        return len(data) == 8 and all(c['state'] == 'succeeded' for c in data.values())
    until(completed)
    assert max(observed) == 2
    assert all(value <= 2 for value in observed)
    assert all(len(c['attempts']) == 1 for c in cases(f, job).values())


def test_failed_mps_termination_retains_process_and_lease(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30), entry(f, 'b', delay=30)])
    until(lambda: all(c['state'] == 'running' for c in cases(f, job).values()) and len(cases(f, job)) == 2)
    record = f.runtime.store.get(job)
    marker = worker.directory(record['mps_spec']) / 'test-refuse-termination'
    marker.touch()
    call(f, 'manage_gpu_job', job_id=job, case_id='a', action='stop')
    until(lambda: f.server._mps_manager().rpc('status', record['mps_spec']).get('paused'))
    data = cases(f, job)
    assert worker.same_process(data['a']['process'])
    assert worker.same_process(data['b']['process'])
    assert f.runtime.store.get(job)['active_reservation']
    assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    result = call(f, 'manage_gpu_job', job_id=job, case_id='a', action='retry')
    assert result['status'] == 'refused'


def test_parent_death_never_releases_live_clients_and_owner_can_stop_group(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30), entry(f, 'b', delay=30)])
    until(lambda: len(cases(f, job)) == 2 and all(c['state'] == 'running' for c in cases(f, job).values()))
    record = f.runtime.store.get(job)
    worker.safe_signal(record['parent'], signal.SIGKILL)
    until(lambda: not worker.same_process(record['parent']))
    result = status(f, job)
    assert result['job_lifecycle'] == 'launch_outcome_unknown'
    assert result['active_reservation']
    metadata = f.server._read_reservation_metadata(record['reservation_key'])
    assert f.server._inspect_reservation_process(metadata)['status'] == 'unknown'
    assert all(worker.same_process(c['process']) for c in cases(f, job).values())
    result = call(f, 'manage_gpu_job', job_id=job, action='stop')
    assert result['status'] == 'stop_queued', result
    until(lambda: f.server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone')
    result = call(f, 'manage_gpu_job', job_id=job, action='finish')
    assert result['status'] == 'finished' and not result['active_reservation']


def test_pid_identity_change_never_sends_a_signal(monkeypatch):
    expected = worker.identity(os.getpid())
    expected['start'] = str(int(expected['start']) + 1)
    monkeypatch.setattr(worker, 'pidfd_open', lambda pid: pytest.fail('must not open a reused PID'))
    assert worker.safe_signal(expected, signal.SIGKILL) is False


def test_lost_launch_reply_is_recovered_without_a_second_launch(mps):
    f = mps
    original = f.server._mps_manager().rpc
    count = []
    from gpu_mcp_mps import MpsManager
    def rpc(self, action, spec, request=None):
        result = original(action, spec, request)
        if action == 'launch':
            count.append(spec['job_id'])
            raise RuntimeError('simulated lost SSH acknowledgement after spawn')
        return result
    f.monkeypatch.setattr(MpsManager, 'rpc', rpc)
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True, runs=[entry(f, 'a', delay=.3)])
    assert result['status'] == 'launch_outcome_unknown'
    until(lambda: status(f, result['job_id'])['job_lifecycle'] == 'succeeded')
    assert len(count) == 1
    assert len(cases(f, result['job_id'])['a']['attempts']) == 1


def test_completion_commit_failure_cannot_free_the_gpu(mps):
    f = mps
    job = launch(f, [entry(f, 'a')])
    record = f.runtime.store.get(job)
    until(lambda: f.server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone')
    original = f.runtime.persist
    def refuse(record):
        if record.get('outcome'):
            raise RuntimeError('injected durable-event transaction failure')
        return original(record)
    f.monkeypatch.setattr(f.runtime, 'persist', refuse)
    assert status(f, job)['status'] == 'refused'
    assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert f.runtime.store.get(job)['active_reservation']
    f.monkeypatch.setattr(f.runtime, 'persist', original)
    assert not status(f, job)['active_reservation']


def test_malformed_control_state_stays_unknown_and_held(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    record = f.runtime.store.get(job)
    worker.safe_signal(record['parent'], signal.SIGKILL)
    until(lambda: not worker.same_process(record['parent']))
    path = worker.directory(record['mps_spec']) / 'state.json'
    original = worker.read_json(path)
    broken = dict(original, cases=[])
    worker.write_json(path, broken)
    try:
        assert status(f, job)['status'] == 'refused'
        assert f.runtime.store.get(job)['active_reservation']
        assert call(f, 'manage_gpu_job', job_id=job, action='finish')['status'] == 'refused'
    finally:
        worker.write_json(path, original)


def test_raw_kill_refuses_mps_even_for_the_owner(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    record = f.runtime.store.get(job)
    result = call(f, 'kill_gpu_process', host='fixture', pid=record['remote_pid'])
    assert result['status'] == 'refused' and 'MPS' in result['reason']
    assert worker.same_process(record['parent'])


def test_main_sweep_uses_existing_smoke_and_cadence_rules(mps):
    f = mps
    smoke = launch(f, [entry(f, 'smoke')], job_title='MPS viability')
    # A result can be saved before process-gone proof releases the GPU.
    until(lambda: status(f, smoke)['job_lifecycle'] == 'succeeded'
          and not f.runtime.store.get(smoke)['active_reservation'])
    # This one-off cannot be substituted for smoke evidence.
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=[entry(f, 'main')], job_role='main', smoke_job_id=smoke)
    assert result['status'] == 'refused'
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=[entry(f, 'valid')], job_role='main', smoke_skip_reason='explicit independent test', cadence_hint_sec=17)
    assert result['status'] == 'launched', result
    assert f.runtime.store.get(result['job_id'])['poll_interval_sec'] == 17


def test_successful_mps_smoke_is_valid_main_evidence(mps):
    f = mps
    smoke = launch(f, [entry(f, 'smoke')], job_role='smoke')
    until(lambda: status(f, smoke)['job_lifecycle'] == 'succeeded')
    assert status(f, smoke)['smoke_evidence'] == {'smoke_job_id':smoke, 'positive_viability_evidence':True}
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=[entry(f, 'main')], job_role='main', smoke_job_id=smoke, cadence_hint_sec=1)
    assert result['status'] == 'launched', result
    basis = f.runtime.store.get(result['job_id'])['cadence_basis']
    assert basis['positive_viability_evidence'] and basis['selected_interval_sec'] == 1


def test_backend_checks_create_short_parent_updates_and_one_case_failure(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=1.3)])
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    record = f.runtime.store.get(job)
    f.runtime.store.save(dict(record, next_poll_after='2000-01-01T00:00:00Z',
                             last_status_checked_at='2000-01-01T00:00:00Z'), origin=f.owner)
    f.runtime.monitor_once()
    events = f.runtime.store.events()
    assert len([e for e in events if e['kind'] == 'case_failed.bad']) == 1
    poll = next(e for e in events if e['kind'] == 'poll')
    assert '1 failed' in poll['payload']['message'] and '1 running' in poll['payload']['message']
    assert len(json.dumps(poll['payload'])) < 800
    assert all(e['origin'] == f.owner.thread_id for e in events)
    f.runtime.deliver_once()
    f.runtime.deliver_once()
    assert len([e for e in f.daemon.sent if e['kind'] == 'case_failed.bad']) == 1


def test_restarted_backend_cannot_adopt_or_stop_live_mps(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    previous = f.server.SERVER_INSTANCE_ID
    f.monkeypatch.setattr(f.server, 'SERVER_INSTANCE_ID', 'server-new-test')
    try:
        assert call(f, 'manage_gpu_job', job_id=job, action='stop')['status'] == 'refused'
        assert call(f, 'manage_gpu_job', job_id=job, case_id='a', action='retry')['status'] == 'refused'
        assert status(f, job)['active_reservation']
        f.runtime.monitor_once()
        assert f.runtime.store.get(job)['server_instance_id'] == previous
        assert any(e['kind'] == 'backend_restarted' for e in f.runtime.store.events())
    finally:
        f.monkeypatch.setattr(f.server, 'SERVER_INSTANCE_ID', previous)


def test_changed_queued_script_cannot_execute_uncaptured_code(mps):
    f = mps
    job = launch(f, [entry(f, 'first', delay=.7), entry(f, 'queued')], max_concurrent=1)
    until(lambda: cases(f, job).get('first', {}).get('state') == 'running')
    (f.repo / 'jobs/queued.py').write_text("from pathlib import Path\nPath('results/unapproved-change').write_text('ran')\n")
    until(lambda: cases(f, job)['queued']['state'] == 'failed')
    assert not (f.repo / 'results/unapproved-change').exists()
    assert 'declared script changed' in Path(cases(f, job)['queued']['output_file']).read_text()


def test_duplicate_retry_request_is_not_another_attempt(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=2)])
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    before = cases(f, job)['bad']
    spec = f.runtime.store.get(job)['mps_spec']
    request = {'request_id':'dedup-fixture', 'case_id':'bad', 'expected_attempt':before['attempt_id'],
               'replacement': {'attempt_id':'case-attempt-' + uuid.uuid4().hex,
                               'output_file':str(f.repo / 'logs/retried.log'), 'script_hash':before['script_hash']}}
    manager = f.server._mps_manager()
    manager.rpc('retry', spec, request=request)
    # Delivery acknowledgement is deliberately ignored; inspect, do not resend
    # as a new request. A duplicate identical pending request is idempotent.
    try:
        manager.rpc('retry', spec, request=request)
    except RuntimeError:
        pass  # Worker already applied it: expected attempt no longer matches.
    until(lambda: len(cases(f, job)['bad']['attempts']) == 2)
    assert len(cases(f, job)['bad']['attempts']) == 2


def test_log_parent_symlink_cannot_escape_the_approved_path(tmp_path):
    inside, outside = tmp_path / 'logs', tmp_path / 'outside'
    inside.mkdir()
    outside.mkdir()
    (inside / 'swapped').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        worker.open_log(inside / 'swapped/job.log')
    assert not (outside / 'job.log').exists()


def test_capability_refusal_happens_before_reservation(mps):
    from gpu_mcp_mps import MpsManager
    f = mps
    def refuse(*args):
        raise ValueError('MPS control not installed')
    f.monkeypatch.setattr(MpsManager, 'capability', refuse)
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True, runs=[entry(f, 'a')])
    assert result['status'] == 'refused'
    assert f.runtime.store.records() == []
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()


def test_case_return_after_parent_death_holds_until_explicit_cleanup(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=.4)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    record = f.runtime.store.get(job)
    process = record['cases'][0]['process']
    worker.safe_signal(record['parent'], signal.SIGKILL)
    time.sleep(.7)  # The script returned; its reporting and command pipes lost their parent.
    assert worker.same_process(process), 'an attached client must not exit on a broken report pipe'
    assert status(f, job)['active_reservation']


@pytest.mark.parametrize('broken', ['partial_process', 'missing_daemon', 'missing_server'])
def test_incomplete_identity_never_proves_a_live_group_gone(mps, broken):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    record = f.runtime.store.get(job)
    worker.safe_signal(record['parent'], signal.SIGKILL)
    until(lambda: not worker.same_process(record['parent']))
    path = worker.directory(record['mps_spec']) / 'state.json'
    original = worker.read_json(path)
    bad = json.loads(json.dumps(original))
    if broken == 'partial_process':
        for name in ('parent','daemon','server'):
            bad[name] = {'pid':bad[name]['pid']}
        bad['cases'][0]['process'] = {'pid':bad['cases'][0]['process']['pid']}
    else:
        bad.pop(broken.removeprefix('missing_'))
    worker.write_json(path, bad)
    try:
        assert call(f, 'manage_gpu_job', job_id=job, action='finish')['status'] == 'refused'
        assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
        assert worker.same_process(original['cases'][0]['process'])
    finally:
        worker.write_json(path, original)


def test_cancelling_waiting_retry_preserves_history_and_allows_explicit_retry(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=2)], max_concurrent=1)
    until(lambda: cases(f, job).get('long', {}).get('state') == 'running')
    (f.repo / 'jobs/bad.py').write_text('print("fixed")\n')
    assert call(f, 'manage_gpu_job', job_id=job, case_id='bad', action='retry')['status'] == 'retry_queued'
    until(lambda: cases(f, job)['bad']['state'] == 'waiting')
    assert call(f, 'manage_gpu_job', job_id=job, case_id='bad', action='stop')['status'] == 'stop_queued'
    until(lambda: cases(f, job)['bad']['state'] == 'cancelled')
    assert call(f, 'manage_gpu_job', job_id=job, case_id='bad', action='retry')['status'] == 'retry_queued'
    until(lambda: cases(f, job)['bad']['state'] == 'succeeded')
    assert [a['state'] for a in cases(f, job)['bad']['attempts']] == ['failed','cancelled','succeeded']


def test_undelivered_child_failure_is_superseded_on_retry(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'long', delay=2)])
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    notice = next(e for e in f.runtime.store.events() if e['kind'] == 'case_failed.bad')
    assert notice['payload']['case_id'] == 'bad'
    assert notice['payload']['case_attempt_id'] == cases(f, job)['bad']['attempt_id']
    f.runtime.store.delivery(notice['event_id'], 'archived')
    (f.repo / 'jobs/bad.py').write_text('print("fixed")\n')
    assert call(f, 'manage_gpu_job', job_id=job, case_id='bad', action='retry')['status'] == 'retry_queued'
    until(lambda: cases(f, job)['bad']['state'] == 'succeeded')
    f.runtime.deliver_once()
    assert f.runtime.store.event(notice['event_id'])['state'] == 'superseded'
    assert not any(e['event_id'] == notice['event_id'] for e in f.daemon.sent)


def test_child_notice_commit_failure_cannot_release_terminal_sweep(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17)])
    record = f.runtime.store.get(job)
    until(lambda: f.server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone')
    with sqlite3.connect(f.runtime.store.path) as db:
        db.execute("CREATE TRIGGER refuse_child BEFORE INSERT ON events WHEN NEW.kind LIKE 'case_failed.%' "
                   "BEGIN SELECT RAISE(ABORT,'child event unavailable'); END")
    f.runtime.monitor_once()
    assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert f.runtime.store.get(job)['active_reservation']
    with sqlite3.connect(f.runtime.store.path) as db:
        db.execute('DROP TRIGGER refuse_child')
    f.runtime.monitor_once()
    until(lambda: not status(f, job)['active_reservation'])
    assert len([e for e in f.runtime.store.events() if e['kind'] == 'case_failed.bad']) == 1


def test_old_record_cannot_update_or_unregister_replacement_booking(mps):
    from test_gpu_mcp_codex_shared import gpu_job
    f = mps
    job = launch(f, [entry(f, 'a')])
    old = f.runtime.store.get(job)
    until(lambda: not status(f, job)['active_reservation'])
    replacement = gpu_job(f)
    path = leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0') / 'metadata.json'
    before = leases.read_json_file_no_follow(path)
    # Recreate a failed post-release persistence write from the old job.
    stale = dict(f.runtime.store.get(job), active_reservation=True)
    f.runtime.store.save(stale, origin=f.owner)
    status(f, job)
    assert leases.read_json_file_no_follow(path) == before
    assert 'fixture.gpu0' in f.server.HEARTBEAT_MANAGER.owned_keys()
    assert not f.runtime.store.get(job)['active_reservation']
    assert f.runtime.store.get(replacement['job_id'])['active_reservation']


def test_finished_orphan_stays_finished_on_later_status(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    record = f.runtime.store.get(job)
    worker.safe_signal(record['parent'], signal.SIGKILL)
    until(lambda: not worker.same_process(record['parent']))
    call(f, 'manage_gpu_job', job_id=job, action='stop')
    call(f, 'manage_gpu_job', job_id=job, action='finish')
    result = status(f, job)
    assert result['job_lifecycle'] == 'finished' and not result['active_reservation']
    assert result['next_poll_after'] is None and result['outcome'] is None


def test_selected_case_respects_not_due_gate(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)], cadence_hint_sec=60)
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    status(f, job)
    from gpu_mcp_mps import MpsManager
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(MpsManager, 'rpc', lambda *a,**k: pytest.fail('cached case read must not contact the host'))
        result = call(f, 'manage_gpu_job', job_id=job, case_id='a', action='status', early_poll_reason='   ')
        assert result['status'] == 'not_due' and result['case']['state'] == 'running'


def test_request_admission_and_shutdown_have_one_order(mps):
    f = mps
    job = launch(f, [entry(f, 'bad', code=17), entry(f, 'last', delay=.6)])
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    before = cases(f, job)['bad']
    spec = f.runtime.store.get(job)['mps_spec']
    path = worker.directory(spec)
    fd = os.open(path / 'requests.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    request = {'request_id':'shutdown-race', 'case_id':'bad', 'expected_attempt':before['attempt_id'],
               'replacement':{'attempt_id':'case-attempt-' + uuid.uuid4().hex,
                              'output_file':str(f.repo / 'logs/race.log'),'script_hash':before['script_hash']}}
    replies = []
    def submit():
        try:
            replies.append(f.server._mps_manager().rpc('retry', spec, request=request))
        except RuntimeError as exc:
            replies.append(exc)
    thread = threading.Thread(target=submit)
    thread.start()
    time.sleep(.9)  # Last case returns while both admission and shutdown wait on the lock.
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    thread.join(timeout=10)
    assert not thread.is_alive() and replies
    until(lambda: f.server._mps_manager().rpc('status', spec)['state'] == 'gone')
    after = cases(f, job)['bad']
    if isinstance(replies[0], dict):
        assert len(after['attempts']) == 2, 'an accepted retry must not disappear during shutdown'
    else:
        assert len(after['attempts']) == 1


def test_cleared_bootstrap_failure_can_reconcile_without_a_cuda_server(mps):
    from gpu_mcp_mps import MpsManager
    f = mps
    f.monkeypatch.setattr(MpsManager, 'capability', lambda *args: {
        'mps_binary':'', 'test_driver':True, 'test_bootstrap_failure':True})
    job = launch(f, [entry(f, 'a')])
    until(lambda: status(f, job)['job_lifecycle'] == 'failed')
    record = f.runtime.store.get(job)
    path = worker.directory(record['mps_spec']) / 'state.json'
    state = worker.read_json(path)
    # Simulated driver creates a server unconditionally; real pre-attachment
    # bootstrap failure may never create one. Every case is proven cleared.
    assert state['cases'][0]['gpu_cleared'] and not state['cases'][0]['connected']
    state.pop('server')
    worker.write_json(path, state)
    assert f.server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone'
    assert not status(f, job)['active_reservation']


def test_one_cuda_bootstrap_failure_does_not_cancel_healthy_siblings(mps):
    from gpu_mcp_mps import MpsManager
    f = mps
    f.monkeypatch.setattr(MpsManager, 'capability', lambda *args: {
        'mps_binary':'', 'test_driver':True, 'test_bootstrap_failure':['bad']})
    job = launch(f, [entry(f, 'bad'), entry(f, 'healthy', delay=1.2), entry(f, 'queued')], max_concurrent=2)
    until(lambda: cases(f, job).get('bad', {}).get('state') == 'failed')
    assert cases(f, job)['healthy']['state'] == 'running'
    assert f.runtime.store.get(job)['active_reservation']
    until(lambda: status(f, job)['job_lifecycle'] == 'failed')
    final = cases(f, job)
    assert final['healthy']['state'] == 'succeeded' and final['queued']['state'] == 'succeeded'
    assert len(final['bad']['attempts']) == 1


def test_shared_lease_contains_no_case_args_or_control_nonce(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)])
    record = f.runtime.store.get(job)
    metadata = f.server._read_reservation_metadata(record['reservation_key'])
    assert 'mps_spec' not in metadata
    assert 'mps_ref' in metadata
    assert not set(metadata['mps_ref']) & {'nonce','runs','args','script_roots','write_roots','output_roots','output_file'}
    reference = metadata['mps_ref']
    assert f.server._mps_manager().rpc('inspect_reference', reference)['state'] == 'alive'


def test_selected_case_inspection_does_not_dump_all_other_cases(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30), entry(f, 'b', delay=30)])
    until(lambda: cases(f, job).get('a', {}).get('state') == 'running')
    result = call(f, 'manage_gpu_job', job_id=job, case_id='a', action='status', detail='full')
    assert result['case']['case_id'] == 'a'
    assert 'mps_spec' not in result['saved_job'] and 'cases' not in result['saved_job']
    assert str(f.repo / 'logs/b.log') not in json.dumps(result)
    assert str(f.repo / 'jobs/b.py') not in json.dumps(result)


def test_large_sweep_uses_compressed_transport_and_one_gpu_reservation(mps):
    f = mps
    base = entry(f, 'task', delay=30)
    entries = [dict(base, case_id=f'case_{i}', args=['--label',str(i)], output_file=f'logs/case_{i}.log') for i in range(250)]
    assert len(json.dumps(entries)) > 15000
    job = launch(f, entries, max_concurrent=1)
    until(lambda: len(cases(f, job)) == 250)
    record = f.runtime.store.get(job)
    assert record['case_counts']['waiting'] >= 249
    assert len(list(leases.reservation_registry_root().glob('*.gpu*'))) == 1
    call(f, 'manage_gpu_job', job_id=job, action='stop')
    until(lambda: status(f, job)['job_lifecycle'] == 'failed')


def test_mps_cadence_and_early_reason_provenance_is_saved(mps):
    f = mps
    job = launch(f, [entry(f, 'a', delay=30)], job_role='main', smoke_skip_reason='independent fixed test')
    result = call(f, 'manage_gpu_job', job_id=job, action='update_cadence', cadence_hint_sec=37,
                  expected_duration_sec=500, reason='planned intermediate measurement')
    assert result['status'] == 'cadence_updated'
    record = f.runtime.store.get(job)
    assert record['smoke_skip_reason'] == 'independent fixed test'
    assert record['expected_duration_sec'] == 500
    assert record['cadence_basis']['selected_interval_sec'] == 37
    assert record['cadence_update_reason'] == 'planned intermediate measurement'
    status(f, job)
    record = f.runtime.store.get(job)
    assert record['last_early_poll_reason'] == 'assert a concrete MPS test condition'
    assert record['last_early_poll_at'] == record['last_status_checked_at']


def test_mps_completion_mirror_cannot_override_protected_outcome(mps):
    import gpu_mcp_policy_hook as hook
    f = mps
    job = launch(f, [entry(f, 'bad', code=17)], cadence_hint_sec=3600)
    record = f.runtime.store.get(job)
    until(lambda: f.server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone')
    assert hook.probe_stop_wait_ownership(f.repo)['owns_stop'] is False
    path = leases.outcome_record_path(f.repo, job, record['active_attempt_id'])
    mirror = leases.read_json_file_no_follow(path)
    mirror.update(terminal_status='success', exit_code=0)
    leases.atomic_write_json(path, mirror)
    with f.runtime.acting_as(f.owner):
        assert f.server._read_outcome_record(record) is None
    assert status(f, job)['job_lifecycle'] == 'failed'


@pytest.fixture
def gpu_probe(mps, monkeypatch):
    """Keep admission real while supplying controlled, read-only GPU observations."""
    from gpu_mcp_mps import MpsManager
    f = mps
    observed = {'mode': 'Default', 'free': 22000, 'processes': 'GPU-selected, 42, /usr/bin/python'}
    monkeypatch.setattr(MpsManager, 'capability', lambda *a: {'mps_binary': '/usr/bin/nvidia-cuda-mps-control'})
    def host_run(host, command, **kwargs):
        if '--query-compute-apps' in command:
            return observed['processes']
        if command == 'nvidia-smi -i 0 -q -x':
            return ('<nvidia_smi_log><gpu><uuid>GPU-selected</uuid><minor_number>3</minor_number>'
                    '<product_name>GPU</product_name><compute_mode>' + observed['mode'] + '</compute_mode>'
                    '<fb_memory_usage><free>' + str(observed['free']) + ' MiB</free></fb_memory_usage>'
                    '</gpu></nvidia_smi_log>')
        raise AssertionError('Admission must refuse before starting processes: ' + command)
    monkeypatch.setattr(f.server, '_host_run', host_run)
    return f, observed


def test_sharing_requires_explicit_choice_and_default_refusal_explains_it(gpu_probe):
    f, observed = gpu_probe
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  runs=[entry(f, 'busy')])
    assert result['status'] == 'refused' and 'mps_allow_existing_cuda=true' in result['reason']
    assert f.runtime.store.records() == []
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    # The same observed card is admissible with explicit ordinary-CUDA sharing.
    uuid, devices = f.server._build_manager().probe_gpu('fixture', 0, allow_existing_cuda=True)
    assert uuid == 'GPU-selected' and '/dev/nvidia3' in devices
    # GPU-assisted builds still use the strict default.
    with pytest.raises(ValueError, match='already has a compute process'):
        f.server._build_manager().probe_gpu('fixture', 0)


@pytest.mark.parametrize('field,value,reason', [
    ('mode', 'Exclusive_Process', 'Default compute mode'),
    ('mode', 'Prohibited', 'Default compute mode'),
    ('mode', '', 'Default compute mode'),
    ('free', 1023, 'insufficient free memory'),
    ('processes', 'GPU-selected, 42, /usr/bin/nvidia-cuda-mps-server', 'MPS server'),
    ('processes', 'GPU-selected, 42, nvidia-cuda-mps-server', 'MPS server'),
    ('processes', 'GPU-selected, 42, [Not Found]', 'cannot identify'),
    ('processes', 'GPU-selected, 42, [N/A]', 'cannot identify'),
    ('processes', 'GPU-selected, 42, [Insufficient Permissions]', 'cannot identify'),
    ('processes', 'unexpected diagnostic instead of a process table', 'cannot identify'),
    ('processes', 'GPU-selected, 42, ', 'cannot identify'),
    ('processes', 'GPU-selected, unknown, /usr/bin/python', 'cannot identify'),
    ('processes', 'GPU-selected, 0, /usr/bin/python', 'cannot identify'),
    ('processes', 'GPU-selected, 42', 'cannot identify'),
    ('processes', None, 'compute-process availability'),
])
def test_shared_admission_refusals_release_only_the_attempted_reservation(gpu_probe, field, value, reason):
    from dataclasses import replace
    f, observed = gpu_probe
    f.server.CONFIG_POLICY = replace(f.server.CONFIG_POLICY, min_free_memory_mib=1024)
    observed[field] = value
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  mps_allow_existing_cuda=True, runs=[entry(f, 'refused')])
    assert result['status'] == 'refused' and reason in result['reason'], result
    assert f.runtime.store.records() == []
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()


def test_shared_probe_is_scoped_to_selected_gpu_and_preserves_model_policy(gpu_probe):
    from dataclasses import replace
    f, observed = gpu_probe
    observed['processes'] += '\nGPU-other, 99, nvidia-cuda-mps-server'
    manager = f.server._build_manager()
    assert manager.probe_gpu('fixture', 0, allow_existing_cuda=True)[0] == 'GPU-selected'
    f.server.CONFIG_POLICY = replace(f.server.CONFIG_POLICY, allowed_gpu_names=('Different GPU',))
    with pytest.raises(ValueError, match='model is not approved'):
        manager.probe_gpu('fixture', 0, allow_existing_cuda=True)


@pytest.mark.parametrize('value', ['yes', 1, None])
def test_sharing_option_requires_a_boolean(mps, value):
    f = mps
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  mps_allow_existing_cuda=value, runs=[entry(f, 'invalid')])
    assert result['status'] == 'refused' and 'must be a boolean' in result['reason']
    assert f.runtime.store.records() == []


def test_sharing_option_requires_mps(mps):
    f = mps
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0,
                  script_path='jobs/task.py', mps_allow_existing_cuda=True)
    assert result['status'] == 'refused' and 'require mps=true' in result['reason']
    assert f.runtime.store.records() == []


def test_shared_launch_records_choice_and_cannot_override_existing_booking(mps):
    f = mps
    launched = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                   mps_allow_existing_cuda=True, runs=[entry(f, 'owned', delay=1)],
                   job_role='one_off', cadence_hint_sec=1)
    assert launched['status'] == 'launched' and launched['mps_allow_existing_cuda'] is True
    job = launched['job_id']
    saved = f.runtime.store.get(job)
    assert saved['mps_allow_existing_cuda'] is True and saved['mps_spec']['allow_existing_cuda'] is True
    reservation = leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0') / 'metadata.json'
    identity = json.loads(reservation.read_text())
    denied = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                  mps_allow_existing_cuda=True, runs=[entry(f, 'overlap')])
    assert denied['status'] == 'refused'
    assert json.loads(reservation.read_text())['job_id'] == identity['job_id']
    until(lambda: status(f, job)['job_lifecycle'] == 'succeeded')
    assert status(f, job)['mps_allow_existing_cuda'] is True
