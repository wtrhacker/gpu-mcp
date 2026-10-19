"""Real session reconnects across an on-disk GPU MCP update, without GPUs."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from gpu_mcp_codex_state import JobStore, Origin, runtime_root
from test_gpu_mcp_codex_transport import SOURCE, record_for, rpc, tool, write_repo

pytestmark = pytest.mark.contract


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    runtime = tmp_path / 'installed'
    runtime.mkdir()
    for path in SOURCE.glob('gpu_mcp*.py'):
        shutil.copy2(path, runtime / path.name)
    # A controlled request proves that refresh respects an in-flight call as
    # well as saved job records. It never starts a worker or contacts a GPU.
    backend = runtime / 'gpu_mcp_backend.py'
    block = '''
@mcp.tool()
@_requires_active_policy
def reconnect_test_wait():
    (REPO_ROOT / 'results/entered').write_text('entered')
    deadline = time.monotonic() + 15
    while not (REPO_ROOT / 'results/release').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test request was not released')
        time.sleep(.02)
    return json.dumps({'status': 'ok'})

'''
    text = backend.read_text()
    backend.write_text(text.replace('if __name__ == "__main__":', block + 'if __name__ == "__main__":'))
    spec = importlib.util.spec_from_file_location('reconnect_' + uuid.uuid4().hex,
                                                runtime / 'gpu_mcp_codex_launcher.py')
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setenv('GPU_MCP_TEST_RESERVATION_ROOT', str(tmp_path / 'leases'))
    monkeypatch.setenv('GPU_MCP_TEST_DISABLE_POLICY_APPROVAL', '1')
    monkeypatch.setenv('GPU_MCP_TEST_SKIP_CODEX_VERSION_CHECK', '1')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'empty-codex-home'))
    policy = write_repo(tmp_path)
    base = tmp_path / 'state'
    info = dict(launcher.ensure_backend(policy, base=base), state_base=str(base))
    store = JobStore(runtime_root(policy, base=base))
    f = SimpleNamespace(runtime=runtime, launcher=launcher, policy=policy, base=base,
                        info=info, store=store, owner=Origin(str(uuid.uuid4())))
    yield f
    # Only this fixture's synthetic records are reconciled here; no live
    # scientific process or external reservation exists in these tests.
    (policy.parent / 'results/release').touch()
    for record in store.records():
        record.update(job_lifecycle='finished', active_reservation=False, next_poll_after=None)
        store.save(record)
    current = launcher.read_info(runtime_root(policy, base=base))
    if launcher.alive(current):
        assert launcher.stop_if_idle(current)


def update_files(f):
    path = f.runtime / 'gpu_mcp_backend.py'
    text = path.read_text()
    addition = '\n@mcp.tool()\n@_requires_active_policy\ndef deployment_probe():\n    return "updated"\n\n'
    path.write_text(text.replace('if __name__ == "__main__":', addition + 'if __name__ == "__main__":'))
    assert f.launcher.code_hash() != f.info['code_hash']


def save_active(f, state):
    record = record_for(f.policy, f.info, f.owner, label='original-owner')
    record.update(job_lifecycle=state, active_reservation=True, outcome=None,
                  next_poll_after='2999-01-01T00:00:00Z')
    # With no launch PID/receipt, the unknown case stays unresolved. The
    # monitor cannot infer completion or acquire a real GPU from this record.
    return f.store.save(record)


def new_session(f):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'reconnect-test', 'version': '1'}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'list_gpu_jobs', 'arguments': {}, '_meta': {'threadId': f.owner.thread_id}}},
    ]
    result = subprocess.run([sys.executable, '-B', str(f.runtime / 'gpu_mcp_codex_launcher.py'),
                             '--config', str(f.policy), '--state-base', str(f.base)],
        input=''.join(json.dumps(m) + '\n' for m in messages), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    replies = {row['id']: row for row in map(json.loads, result.stdout.splitlines())}
    assert set(replies) == {1, 2, 3} and all('result' in r for r in replies.values()), result.stderr
    return replies, result.stderr


@pytest.mark.parametrize('state', ['running', 'launch_outcome_unknown'])
def test_restarted_session_can_manage_existing_jobs_after_files_change(deployment, state):
    f = deployment
    record = save_active(f, state)
    update_files(f)
    replies, diagnostic = new_session(f)
    assert 'update pending' in diagnostic
    tools = replies[2]['result']['tools']
    assert any(t['name'] == 'manage_gpu_job' for t in tools)
    assert not any(t['name'] == 'deployment_probe' for t in tools)
    jobs = json.loads(replies[3]['result']['content'][0]['text'])['jobs']
    assert [job['job_id'] for job in jobs] == [record['job_id']]
    current = f.launcher.read_info(runtime_root(f.policy, base=f.base))
    assert current['server_instance_id'] == f.info['server_instance_id']
    saved = f.store.get(record['job_id'])
    assert saved['origin_thread_id'] == f.owner.thread_id and saved['active_reservation']
    foreign = tool(current, 'manage_gpu_job', {'job_id': record['job_id']}, str(uuid.uuid4()))
    assert foreign['status'] == 'refused'


def test_next_idle_connection_loads_new_code_and_keeps_history_and_events(deployment):
    f = deployment
    record = record_for(f.policy, f.info, f.owner, label='completed-before-update')
    f.store.notice(record, 'milestone', 'retained checkpoint', key='before-update')
    event_ids = {r['event_id'] for r in f.store.events()}
    update_files(f)
    replies, _ = new_session(f)
    assert any(t['name'] == 'deployment_probe' for t in replies[2]['result']['tools'])
    current = f.launcher.read_info(runtime_root(f.policy, base=f.base))
    assert current['server_instance_id'] != f.info['server_instance_id']
    assert current['code_hash'] == f.launcher.code_hash()
    assert not f.launcher.alive(f.info)
    jobs = json.loads(replies[3]['result']['content'][0]['text'])['jobs']
    assert [job['job_id'] for job in jobs] == [record['job_id']]
    assert event_ids <= {r['event_id'] for r in f.store.events()}


def test_active_request_defers_update_and_next_connection_refreshes(deployment):
    f = deployment
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(tool, f.info, 'reconnect_test_wait', {}, f.owner.thread_id)
        try:
            deadline = time.monotonic() + 5
            while not (f.policy.parent / 'results/entered').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (f.policy.parent / 'results/entered').exists()
            update_files(f)
            attached = f.launcher.ensure_backend(f.policy, base=f.base)
            assert attached['server_instance_id'] == f.info['server_instance_id']
        finally:
            (f.policy.parent / 'results/release').touch()
        assert pending.result(timeout=5)['status'] == 'ok'
    attached = f.launcher.ensure_backend(f.policy, base=f.base)
    assert attached['server_instance_id'] != f.info['server_instance_id']


def test_concurrent_reconnects_refresh_idle_backend_only_once(deployment):
    f = deployment
    update_files(f)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: f.launcher.ensure_backend(f.policy, base=f.base), range(4)))
    assert len({r['server_instance_id'] for r in replies}) == 1
    assert replies[0]['server_instance_id'] != f.info['server_instance_id']


def test_status_reports_file_update_without_refreshing_or_exposing_credentials(deployment):
    f = deployment
    update_files(f)
    result = subprocess.run([sys.executable, '-B', str(f.runtime / 'gpu_mcp_codex_launcher.py'),
                             '--config', str(f.policy), '--state-base', str(f.base), '--status'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status['status'] == 'running' and status['update_pending']
    assert status['backend_code_hash'] == f.info['code_hash']
    assert status['installed_code_hash'] == f.launcher.code_hash()
    assert f.launcher.alive(f.info)
    assert not {'agent_token', 'human_token'} & status.keys()
    absent = f.policy.parent.parent / 'absent/gpu-mcp.toml'
    root = runtime_root(absent, base=f.base)
    assert f.launcher.backend_status(absent, base=f.base)['status'] == 'not_running'
    assert not root.exists()


def test_code_update_does_not_bypass_protocol_or_runtime_identity(deployment, monkeypatch):
    f = deployment
    update_files(f)
    monkeypatch.setattr(f.launcher, 'PROTOCOL_VERSION', 999)
    with pytest.raises(RuntimeError, match='incompatible connection protocol'):
        f.launcher.ensure_backend(f.policy, base=f.base)
    monkeypatch.setattr(f.launcher, 'PROTOCOL_VERSION', f.info['protocol_version'])
    monkeypatch.setenv('GPU_MCP_PYTHON', '/different-interpreter')
    with pytest.raises(RuntimeError, match='different runtime settings'):
        f.launcher.ensure_backend(f.policy, base=f.base)
    assert f.launcher.alive(f.info)
