"""Invalid launch-state paths must fail before booking a GPU or contacting SSH."""
from pathlib import Path

import pytest

import gpu_mcp_reservations as leases
from test_gpu_mcp_codex_shared import call, gpu_job, shared

pytestmark = pytest.mark.contract


@pytest.mark.parametrize('obstruction', ['state-file', 'jobs-file', 'state-link', 'jobs-link'])
def test_blocked_state_path_cannot_create_an_unknown_launch_or_a_reservation(shared, obstruction):
    f = shared
    state = f.repo / '.gpu_mcp_state'
    # Model cleanup removing the empty targets after a backend was started.
    if state.is_dir():
        (state / 'jobs').rmdir()
        state.rmdir()
    if obstruction.startswith('jobs'):
        state.mkdir()
        path = state / 'jobs'
    else:
        path = state
    if obstruction.endswith('link'):
        target = f.repo / 'results/unrelated'
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
    else:
        path.write_bytes(b'')
        path.chmod(0o444)
    calls = []
    f.monkeypatch.setattr(f.server, '_ssh_run', lambda *args, **kwargs: calls.append(args))
    result = call(f, 'run_python_on_gpu', host='fixture', gpu_index=0,
                  script_path='jobs/task.py', job_role='smoke')
    assert result['status'] == 'refused', result
    assert 'state' in result['reason'].lower(), result
    assert calls == [], 'a known local setup failure must never reach the remote launcher'
    assert f.runtime.store.records() == []
    assert not f.server.HEARTBEAT_MANAGER.owned_keys()
    assert not leases.reservation_dir(leases.reservation_registry_root(), 'fixture.gpu0').exists()
    assert path.is_symlink() if obstruction.endswith('link') else path.is_file()


def test_preexisting_state_directories_are_not_jobs_or_a_reservation(shared):
    f = shared
    leases.ensure_repo_job_state_root(f.repo)
    leases.ensure_repo_job_state_root(f.repo)
    assert (f.repo / '.gpu_mcp_state/jobs').is_dir()
    assert list((f.repo / '.gpu_mcp_state/jobs').iterdir()) == []
    assert f.runtime.store.records() == []


def test_retry_with_blocked_state_keeps_the_original_attempt_and_lease(shared):
    f = shared
    job = gpu_job(f)
    state = f.repo / '.gpu_mcp_state'
    state.rename(f.repo / 'results/prior-state')
    state.write_bytes(b'')
    state.chmod(0o444)
    calls = []
    f.monkeypatch.setattr(f.server, '_inspect_reservation_process', lambda metadata: {'status': 'gone'})
    f.monkeypatch.setattr(f.server, '_ssh_run', lambda *args, **kwargs: calls.append(args))
    result = call(f, 'manage_gpu_job', action='retry', job_id=job['job_id'])
    assert result['status'] == 'refused', result
    assert 'state' in result['reason'].lower()
    assert calls == []
    assert f.runtime.store.get(job['job_id'])['active_attempt_id'] == job['active_attempt_id']
    assert leases.reservation_dir(leases.reservation_registry_root(), job['reservation_key']).exists()
