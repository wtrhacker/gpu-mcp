"""Upgrade recovery pins its process and retains active jobs and receipts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import uuid

import pytest

import gpu_mcp_ssh_recovery as recovery
import gpu_mcp_reservations as reservations
from gpu_mcp_codex_state import JobStore, Origin, runtime_root

pytestmark = pytest.mark.contract


@pytest.fixture
def running(tmp_path, monkeypatch):
    repo, runtime, home = tmp_path / 'repo', tmp_path / 'runtime', tmp_path / 'home'
    (repo / '.codex').mkdir(parents=True)
    runtime.mkdir()
    config = repo / 'gpu-mcp.toml'
    config.write_text('schema_version=1\n')
    base = home / 'gpu-mcp/state/codex'
    root = runtime_root(config, base=base)
    store = JobStore(root)
    script = runtime / 'gpu_mcp_server.py'
    # The worker shares its parent's process group. killpg would kill it too.
    worker_script = runtime / 'worker.py'
    worker_script.write_text('import time\nwhile True: time.sleep(1)\n')
    worker_pid = tmp_path / 'worker.pid'
    script.write_text('''import subprocess,sys,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
p=subprocess.Popen([sys.executable,str(Path(__file__).with_name('worker.py'))])
Path(sys.argv[-1]).write_text(str(p.pid))
while True: time.sleep(1)
''')
    (runtime / 'gpu_mcp_codex_launcher.py').write_text('# fixture launcher\n')
    reg = repo / '.codex/config.toml'
    reg.write_text('[mcp_servers.gpu-cluster-mcp]\ncommand=' + json.dumps(sys.executable) +
                   '\nargs=' + json.dumps([str(runtime / 'gpu_mcp_codex_launcher.py'), '--config', str(config)]) + '\n')
    child = subprocess.Popen([sys.executable, str(script), '--config', str(config), '--codex-shared',
                              '--runtime-root', str(root), str(worker_pid)], start_new_session=True,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    deadline = time.monotonic() + 5
    while not worker_pid.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert worker_pid.exists()
    owner = reservations.generate_server_instance_id(pid=child.pid)
    info = dict(pid=child.pid, process_start=recovery.launcher.process_start(child.pid),
                server_instance_id=owner, config_path=str(config), hostname=socket.gethostname(),
                code_hash='previous', runtime_signature='fixture')
    reservations.atomic_write_json(root / 'backend.json', info)
    job = {'job_id': reservations.generate_job_id(), 'active_attempt_id': reservations.generate_attempt_id(),
           'repo_root': str(repo), 'server_instance_id': owner, 'job_lifecycle': 'running',
           'active_reservation': True, 'host': 'fixture', 'gpu_index': 0, 'remote_pid': int(worker_pid.read_text())}
    saved = store.save(job, origin=Origin(str(uuid.uuid4())), register=True)
    store.notice(saved, 'milestone', 'unchanged receipt', key='fixture')
    metadata = tmp_path / 'registry/fixture.gpu0/metadata.json'
    reservations.atomic_write_json(metadata, dict(job, owner_user='fixture'))
    plan = dict(config=config, root=root, runtime=runtime, python=sys.executable, server={})
    monkeypatch.setattr(recovery, 'resources', lambda pid: {'threads': 1000, 'sockets': 1000, 'descriptors': 1010})
    monkeypatch.setattr(recovery, 'healthy', lambda info: False)
    stopped = recovery.stop_exhausted
    monkeypatch.setattr(recovery, 'stop_exhausted', lambda plan, info: stopped(plan, info, grace=.05))
    monkeypatch.setattr(recovery, 'start_backend', lambda plan: {'server_instance_id': 'replacement'})
    result = SimpleNamespace(repo=repo, runtime=runtime, home=home, config=config, root=root, store=store,
                             child=child, worker_pid=int(worker_pid.read_text()), metadata=metadata,
                             info=info, plan=plan, registration=reg)
    try:
        yield result
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        try:
            os.kill(result.worker_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_exhausted_backend_is_replaced_but_worker_database_and_reservation_survive(running):
    f = running
    database = (f.root / 'jobs.sqlite3').read_bytes()
    booking = f.metadata.read_bytes()
    worker_start = recovery.launcher.process_start(f.worker_pid)
    result = recovery.repair(f.runtime, [f.repo], home=f.home)
    assert result['repaired'] == [str(f.repo)]
    assert f.child.wait(timeout=5) == -signal.SIGKILL
    os.kill(f.worker_pid, 0)
    assert worker_start and recovery.launcher.process_start(f.worker_pid) == worker_start
    assert (f.root / 'jobs.sqlite3').read_bytes() == database
    assert f.metadata.read_bytes() == booking
    audit = json.loads(next((f.root / 'repairs').glob('*.json')).read_text())
    assert audit['state'] == 'restarted'
    assert audit['preserved_jobs'] == [f.store.records()[0]['job_id']]
    assert f.store.records()[0]['server_instance_id'] == f.info['server_instance_id']
    assert f.store.events()[0]['state'] == 'pending'


def test_preview_does_not_stop_create_audit_or_change_saved_work(running):
    f = running
    before = f.registration.read_bytes(), f.metadata.read_bytes(), (f.root / 'jobs.sqlite3').read_bytes()
    result = recovery.repair(f.runtime, [f.repo], home=f.home, preview=True)
    assert result['preview'] and not result['repaired']
    assert f.child.poll() is None and not (f.root / 'repairs').exists()
    assert before == (f.registration.read_bytes(), f.metadata.read_bytes(), (f.root / 'jobs.sqlite3').read_bytes())


@pytest.mark.parametrize('change', ['pid', 'process_start', 'server_instance_id', 'config_path'])
def test_metadata_identity_change_never_signals_a_process(running, monkeypatch, change):
    f = running
    altered = dict(f.info, **{change: 'replacement'})
    reservations.atomic_write_json(f.root / 'backend.json', altered)
    monkeypatch.setattr(recovery, 'pidfd_signal', lambda *a: pytest.fail('identity mismatch was signalled'))
    with pytest.raises(ValueError, match='identity changed'):
        recovery.verify_process(f.plan, f.info)


def test_registered_interpreter_mismatch_is_not_a_signal_target(running):
    plan = dict(running.plan, python='/not/the/registered/python')
    with pytest.raises(ValueError, match='interpreter differs'):
        recovery.verify_process(plan, running.info)


def test_healthy_busy_backend_is_deferred_without_a_signal(running, monkeypatch):
    f = running
    monkeypatch.setattr(recovery, 'healthy', lambda info: True)
    monkeypatch.setattr(recovery.launcher, 'request', lambda *a, **k: {'server_instance_id':f.info['server_instance_id'], 'jobs':f.store.records()})
    monkeypatch.setattr(recovery, 'stop_exhausted', lambda *a: pytest.fail('healthy backend was forced down'))
    result = recovery.repair(f.runtime, [f.repo], home=f.home)
    assert result['deferred'] == [str(f.repo)] and not result['repaired']
    assert f.child.poll() is None


def test_wrong_runtime_and_legacy_registrations_are_left_for_normal_resume(running):
    f = running
    assert recovery.repair(f.runtime / 'other', [f.repo], home=f.home)['repaired'] == []
    f.registration.write_text(f.registration.read_text().replace('gpu_mcp_codex_launcher.py', 'gpu_mcp_server.py'))
    assert recovery.repair(f.runtime, [f.repo], home=f.home)['repaired'] == []
    assert f.child.poll() is None


def test_invalid_protected_job_state_blocks_forced_recovery(running, monkeypatch):
    f = running
    with f.store._connect() as db:
        db.execute('UPDATE jobs SET record=?', ('{"job_id":"unidentified"}',))
    monkeypatch.setattr(recovery, 'stop_exhausted', lambda *a: pytest.fail('unrecoverable job state was discarded'))
    with pytest.raises(ValueError, match='protected job identity'):
        recovery.repair(f.runtime, [f.repo], home=f.home)
    assert f.child.poll() is None


def test_changed_registration_is_detected_before_signalling(running, monkeypatch):
    f = running
    inspect = recovery.inspect_backend

    def concurrent(plan):
        result = inspect(plan)
        f.registration.write_text(f.registration.read_text() + '# concurrent edit\n')
        return result

    monkeypatch.setattr(recovery, 'inspect_backend', concurrent)
    with pytest.raises(ValueError, match='registration changed'):
        recovery.repair(f.runtime, [f.repo], home=f.home)
    assert f.child.poll() is None


def test_recovered_health_is_rechecked_after_pinning_the_pid(running, monkeypatch):
    f = running
    monkeypatch.setattr(recovery, 'healthy', lambda info: True)
    monkeypatch.setattr(recovery, 'pidfd_signal', lambda *a: pytest.fail('recovered backend was signalled'))
    with pytest.raises(ValueError, match='recovered'):
        recovery.stop_exhausted(f.plan, f.info)


def test_authentication_error_is_not_treated_as_an_exhausted_backend(monkeypatch):
    def refused(*a, **k):
        raise urllib.error.HTTPError('http://fixture', 403, 'forbidden', {}, None)
    monkeypatch.setattr(recovery.launcher, 'request', refused)
    with pytest.raises(ValueError, match='credentials were refused'):
        recovery.healthy({})


def test_identity_is_rechecked_after_pidfd_open(running, monkeypatch):
    f = running
    open_pidfd = recovery.pidfd_open

    def replaced(pid):
        fd = open_pidfd(pid)
        reservations.atomic_write_json(f.root / 'backend.json', dict(f.info, server_instance_id='replacement'))
        return fd

    monkeypatch.setattr(recovery, 'pidfd_open', replaced)
    monkeypatch.setattr(recovery, 'pidfd_signal', lambda *a: pytest.fail('changed owner was signalled'))
    with pytest.raises(ValueError, match='identity changed'):
        recovery.stop_exhausted(f.plan, f.info)
    assert f.child.poll() is None


def test_process_inventory_finds_legacy_and_shared_copies_but_excludes_gpu_workers(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()

    def process(pid, argv, *, state='S'):
        root = proc / str(pid)
        (root / 'fd').mkdir(parents=True)
        (root / 'exe').symlink_to(Path(sys.executable).resolve())
        (root / 'cmdline').write_bytes(b'\0'.join(value.encode() for value in argv) + b'\0')
        (root / 'status').write_text('Threads:\t3\n')
        fields = [state] + ['0'] * 18 + ['12345']
        (root / 'stat').write_text(f'{pid} (python) ' + ' '.join(fields))
        (root / 'fd/4').symlink_to('socket:[1234]')

    script = '/installed/gpu_mcp_server.py'
    process(100, ['/python',script,'--config','/project/a/gpu-mcp.toml'])
    process(101, ['/python','-B','/other/gpu_mcp_server.py','--config','/project/b/gpu-mcp.toml','--codex-shared'])
    process(102, ['/python',script,'--safe-run','--config','/project/worker/gpu-mcp.toml'])
    process(103, ['/python','-c',script,'--config','/project/unrelated/gpu-mcp.toml'])
    process(104, ['/python',script,'--config','/project/zombie/gpu-mcp.toml'], state='Z')
    rows = recovery.running_servers(proc_root=proc)
    assert {p['pid'] for p in rows} == {100, 101}
    assert {p['runtime'] for p in rows} == {'/installed','/other'}
    assert all(p['resources']['sockets'] == 1 for p in rows)
    assert {p['repo_root'] for p in rows} == {'/project/a','/project/b'}
