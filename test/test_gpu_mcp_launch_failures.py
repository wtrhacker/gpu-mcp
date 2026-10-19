"""Known failures before process creation release their claims; silence does not."""
import json
from pathlib import Path
import subprocess

import pytest

import gpu_mcp_mps_worker as worker
import gpu_mcp_reservations as leases
from test_gpu_mcp_codex_shared import call, shared
from test_gpu_mcp_mps import mps, entry, status

pytestmark = pytest.mark.contract


@pytest.mark.parametrize('mps_mode', [False, True])
@pytest.mark.parametrize('observation', ['missing', None])
def test_unavailable_execution_repo_is_refused_before_any_booking(shared, monkeypatch, mps_mode, observation):
    f = shared
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    monkeypatch.setattr(f.server, '_host_run', lambda *a, **k: observation)
    args = {'mps': True, 'runs': [entry(f, 'missing')]} if mps_mode else {'script_path': 'jobs/task.py'}
    reply = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, **args)
    assert reply['status'] == 'refused' and 'no GPU was reserved' in reply['reason']
    assert f.runtime.store.records() == []
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()


def test_real_remote_launcher_reports_missing_cwd_and_releases_recorded_claim(shared, monkeypatch):
    f = shared
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    monkeypatch.setattr(f.server, '_host_run', lambda *a, **k: 'ready')
    # Simulate a mount disappearing after preflight. Run the actual trusted
    # launch command in a separate Python, without starting a scientific worker.
    original = f.server._remote_async_launch_command
    def command(*a, **kw):
        with monkeypatch.context() as change:
            change.setattr(f.server, 'REPO_ROOT', f.repo / 'missing-on-execution-host')
            return original(*a, **kw)
    monkeypatch.setattr(f.server, '_remote_async_launch_command', command)
    commands = []
    def ssh(host, cmd):
        commands.append(cmd)
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return result.stdout
    monkeypatch.setattr(f.server, '_ssh_run', ssh)
    reply = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, script_path='jobs/task.py')
    assert reply['status'] == reply['job_lifecycle'] == 'failed', reply
    assert reply['active_reservation'] is False
    record = f.runtime.store.get(reply['job_id'])
    assert record['failed_before_start'] and record['outcome']['terminal_status'] == 'launcher_error'
    assert record.get('remote_pid') is None and len(commands) == 1
    assert any(e['kind'] == 'failed' for e in f.runtime.store.events())
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    final = call(f, 'manage_gpu_job', job_id=reply['job_id'], action='status')
    assert final['job_lifecycle'] == 'failed' and final['management_complete']


def test_silent_ssh_launch_is_not_assumed_to_have_failed(shared, monkeypatch):
    f = shared
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    monkeypatch.setattr(f.server, '_host_run', lambda *a, **k: 'ready')
    monkeypatch.setattr(f.server, '_ssh_run', lambda *a, **k: None)
    reply = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, script_path='jobs/task.py')
    assert reply['status'] == 'launch_outcome_unknown'
    assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert not f.runtime.store.get(reply['job_id']).get('failed_before_start')


@pytest.mark.parametrize('lose_reply', [False, True])
def test_mps_log_setup_failure_releases_even_if_its_reply_is_lost(mps, monkeypatch, lose_reply):
    from gpu_mcp_mps import MpsManager
    f = mps
    original = MpsManager.rpc
    def rpc(self, action, spec, request=None):
        if action != 'launch':
            return original(self, action, spec, request)
        # The controller saw this folder; the execution host now cannot.
        logs = f.repo / 'logs'
        logs.rmdir()
        try:
            result = original(self, action, spec, request)
        finally:
            logs.mkdir()
        if lose_reply:
            raise RuntimeError('SSH reply lost after the protected failure receipt was written')
        return result
    monkeypatch.setattr(MpsManager, 'rpc', rpc)
    reply = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, mps=True,
                 runs=[entry(f, 'not-started')], job_role='one_off', cadence_hint_sec=1)
    if lose_reply:
        assert reply['status'] == 'launch_outcome_unknown'
        assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    else:
        assert reply['status'] == 'failed'
    final = status(f, reply['job_id'])
    assert final['job_lifecycle'] == 'failed' and not final['active_reservation']
    record = f.runtime.store.get(reply['job_id'])
    proof = worker.inspect(record['mps_spec'])
    assert proof['state'] == 'gone' and proof['live_processes'] == []
    assert proof['launch_failure']['job_id'] == reply['job_id']
    assert record['outcome']['terminal_status'] == 'launcher_error'
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert [e['kind'] for e in f.runtime.store.events()].count('failed') == 1


def test_failure_receipt_for_another_attempt_is_not_accepted(shared):
    f = shared
    value = json.dumps({'status': 'launch_failed', 'job_id': 'job-one',
                        'attempt_id': 'attempt-old', 'error': 'missing directory'})
    assert f.server._prestart_failure(value, 'job-one', 'attempt-new') is None



def test_repository_ack_file_cannot_authorize_releasing_an_uncertain_launch(shared, monkeypatch):
    f = shared
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    monkeypatch.setattr(f.server, '_host_run', lambda *a, **k: 'ready')
    original = f.server._remote_async_launch_command
    def command(*args, **kwargs):
        # The research directory is not controller authority. Even a matching
        # negative acknowledgement there cannot prove that no worker started.
        base = args[4]
        path = kwargs['pid_ack_path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'status':'launch_failed', 'job_id':base['job_id'],
                                   'attempt_id':base['attempt_id'], 'error':'untrusted negative reply'}))
        return original(*args, **kwargs)
    monkeypatch.setattr(f.server, '_remote_async_launch_command', command)
    monkeypatch.setattr(f.server, '_ssh_run', lambda *a, **k: None)
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, script_path='jobs/task.py')
    assert result['status'] == 'launch_outcome_unknown'
    assert leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert not f.runtime.store.get(result['job_id']).get('failed_before_start')


@pytest.mark.parametrize('exit_code', [0, 17])
def test_real_job_completion_keeps_normal_success_and_failure_cleanup(shared, monkeypatch, exit_code):
    from gpu_mcp_codex_launcher import process_start
    from test_gpu_mcp_mps import until
    f = shared
    (f.repo/'jobs/task.py').write_text(f'print("actual worker ran")\nraise SystemExit({exit_code})\n')
    monkeypatch.setattr(f.server, '_is_local_host', lambda host: False)
    monkeypatch.setattr(f.server, '_host_run', lambda *a, **k: 'ready')
    def ssh(host, command):
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        return result.stdout
    monkeypatch.setattr(f.server, '_ssh_run', ssh)
    # Inspect actual test process exit, rather than returning a canned outcome.
    # This guarded worker performs no CUDA work, so it needs no physical GPU.
    monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata: {
        'status':'alive' if process_start(metadata['remote_pid']) is not None else 'gone',
        'reason':'actual local worker process observation', 'remote_pid':metadata['remote_pid']})
    launched = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0, script_path='jobs/task.py',
                    output_file='logs/actual-worker.log')
    assert launched['status'] == 'launched', launched
    def finished():
        result = call(f, 'manage_gpu_job', job_id=launched['job_id'], action='status',
                      early_poll_reason='verify actual worker completion and release')
        return result if result.get('management_complete') else None
    final = until(finished)
    assert final['job_lifecycle'] == ('succeeded' if exit_code==0 else 'failed')
    record = f.runtime.store.get(launched['job_id'])
    assert record['outcome']['exit_code'] == exit_code
    assert record['outcome']['terminal_status'] == ('success' if exit_code==0 else 'failure')
    assert not record.get('failed_before_start')
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert 'actual worker ran' in (f.repo/'logs/actual-worker.log').read_text()
