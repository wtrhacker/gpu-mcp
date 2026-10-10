"""Reproduce observed uncertainty with real launches and process probes.

No GPU computation, SSH or Codex model is used. Each test owns its processes,
policy, reservation registry and authority. Process identity and launch metadata
are produced by the actual server code, rather than prefilled job fixtures.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from gpu_mcp_codex_runtime import CodexRuntime
from gpu_mcp_codex_state import Origin
from gpu_mcp_monitor_display import task_row
import gpu_mcp_reservations as reservations

pytestmark = pytest.mark.contract
SOURCE = Path(__file__).resolve().parents[1]


class NoDelivery:
    def close(self):
        pass


@pytest.fixture
def launched_server(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    for name in ('jobs', 'results', 'logs'):
        (repo / name).mkdir(parents=True)
    (repo / 'jobs/probe.py').write_text("import time\ntime.sleep(0.1)\nprint('harmless local probe')\n")
    policy = repo / 'gpu-mcp.toml'
    policy.write_text(f"schema_version = 1\nrepo_root = {str(repo)!r}\nnodes = ['localhost']\n"
                      "script_roots = ['jobs']\nwrite_roots = ['results']\noutput_roots = ['logs']\n"
                      "min_free_memory_mib = 0\nsync_timeout_sec = 2\n")
    monkeypatch.setenv('GPU_MCP_TEST_RESERVATION_ROOT', str(tmp_path / 'reservations'))
    monkeypatch.setenv('GPU_MCP_PYTHON', sys.executable)
    monkeypatch.setattr(sys, 'argv', ['gpu_mcp_server.py', '--config', str(policy)])
    name = 'uncertainty_repro_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, SOURCE / 'gpu_mcp_server.py')
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, server)
    spec.loader.exec_module(server)
    runtime = CodexRuntime(server, tmp_path / 'authority', daemon=NoDelivery())
    server.CODEX_RUNTIME = runtime
    owner = Origin(str(uuid.uuid4()))
    monkeypatch.setattr(server.mcp, 'get_context', lambda: SimpleNamespace(
        request_context=SimpleNamespace(meta={'threadId': owner.thread_id})))
    # Only physical GPU usage is absent. ps and the Python identity probe are real.
    monkeypatch.setattr(server, '_process_gpu_usage', lambda *args, **kwargs: (None, None))
    yield SimpleNamespace(server=server, runtime=runtime, repo=repo, owner=owner, monkeypatch=monkeypatch)
    runtime.stop()
    server.HEARTBEAT_MANAGER.stop()


def launch(fixture, *, exit_code=0, check_interval=1):
    (fixture.repo / 'jobs/probe.py').write_text(
        f"import time\ntime.sleep(0.1)\nprint('harmless local probe')\nraise SystemExit({exit_code})\n")
    result = json.loads(fixture.server.run_python_on_gpu(
        host='localhost', gpu_index=0, script_path='jobs/probe.py',
        job_role='one_off', cadence_hint_sec=check_interval, job_title='Delayed result reproduction'))
    assert result['status'] in {'launched', 'launched_with_warning'}, result
    return fixture.runtime.store.get(result['job_id'])


def wait_until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError('the real local probe did not finish within five seconds')


def test_actual_identity_probe_recognizes_live_matching_process(launched_server):
    f = launched_server
    marker = 'gpu-mcp-process:isolated-identity-probe'
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(20)'],
                             env=dict(os.environ, GPU_MCP_PROCESS_FINGERPRINT=marker),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = f.server._inspect_reservation_process({
            'host': 'localhost', 'remote_pid': child.pid, 'owner_user': f.server.GPU_MCP_USER,
            'server_instance_id': f.server.SERVER_INSTANCE_ID, 'reservation_key': 'localhost.gpu0',
            'process_fingerprint': marker, 'remote_start_time': None})
        errors = [value for value in f.server.HOST_RUN_ERRORS.values() if 'SyntaxError' in value]
        print('Identity probe:', result, 'generated-command errors:', errors)
        assert result['status'] == 'alive', result
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_real_gpu_launch_records_held_booking_for_monitor(launched_server):
    f = launched_server
    record = launch(f)
    metadata = reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json'
    assert metadata.exists()
    print('Booking on disk:', metadata.exists(), 'saved flag:', record.get('active_reservation'),
          'display:', task_row(record)['reservation'])
    assert record.get('active_reservation') is True
    assert task_row(record)['reservation'] == 'held'
    wait_until(lambda: f.server._read_outcome_record(record))


@pytest.mark.parametrize('exit_code, expected', [(0, 'succeeded'), (17, 'failed')])
@pytest.mark.parametrize('directory_visible', [False, True], ids=['late-directory-entry', 'late-file-read'])
def test_real_launch_recovers_when_result_visibility_is_delayed(launched_server, exit_code, expected, directory_visible):
    f = launched_server
    record = launch(f, exit_code=exit_code)
    actual_read = f.server._read_outcome_record
    wait_until(lambda: actual_read(record))
    metadata = reservations.read_json_file_no_follow(
        reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json')
    wait_until(lambda: f.server._inspect_reservation_process(metadata)['status'] == 'gone')

    visible = {'value': False}
    actual_lstat = Path.lstat
    outcome_path = reservations.outcome_record_path(f.repo, record['job_id'], record['active_attempt_id'])

    def observed_lstat(path, *args, **kwargs):
        if path == outcome_path and not directory_visible and not visible['value']:
            raise FileNotFoundError('simulated delayed shared-filesystem visibility')
        return actual_lstat(path, *args, **kwargs)

    f.monkeypatch.setattr(Path, 'lstat', observed_lstat)
    f.monkeypatch.setattr(f.server, '_read_outcome_record', lambda r: actual_read(r) if visible['value'] else None)
    clock = {'seconds': 2}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz or timezone.utc) + timedelta(seconds=clock['seconds'])

    f.monkeypatch.setattr(f.server, 'datetime', Clock)
    f.runtime.monitor_once()
    before = f.runtime.store.get(record['job_id'])
    assert before['job_lifecycle'] == 'process_gone_unknown_outcome'

    clock['seconds'] += 10
    visible['value'] = True
    assert actual_read(record)['exit_code'] == exit_code
    f.runtime.monitor_once()
    after = f.runtime.store.get(record['job_id'])
    print('After ten simulated seconds:', {'before': before['job_lifecycle'],
          'file_exit_code': actual_read(record)['exit_code'], 'after': after['job_lifecycle'],
          'booking_exists': (reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json').exists()})
    assert after['job_lifecycle'] == expected
    assert after['outcome']['exit_code'] == exit_code
    assert after['active_reservation'] is False
    assert not (reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json').exists()


def test_prior_shared_record_without_booking_flag_still_recovers(launched_server):
    f = launched_server
    record = launch(f)
    wait_until(lambda: f.server._read_outcome_record(record))
    metadata = reservations.read_json_file_no_follow(
        reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json')
    wait_until(lambda: f.server._inspect_reservation_process(metadata)['status'] == 'gone')
    record = dict(record, job_lifecycle='process_gone_unknown_outcome', next_poll_after=None)
    record.pop('active_reservation')
    f.runtime.store.save(record, origin=f.owner)
    f.runtime.monitor_once()
    saved = f.runtime.store.get(record['job_id'])
    assert saved['job_lifecycle'] == 'succeeded'
    assert saved['active_reservation'] is False


def test_unknown_booking_state_is_never_displayed_as_released():
    row = task_row({'job_id': 'fixture', 'gpu_index': 0, 'job_lifecycle': 'process_gone_unknown_outcome'})
    assert row['reservation'] == 'unknown'


def test_visible_result_that_becomes_readable_finishes_before_next_poll(launched_server):
    f = launched_server
    record = launch(f, check_interval=3600)
    actual_read = f.server._read_outcome_record
    wait_until(lambda: actual_read(record))
    metadata = reservations.read_json_file_no_follow(
        reservations.reservation_dir(reservations.reservation_registry_root(), record['reservation_key']) / 'metadata.json')
    wait_until(lambda: f.server._inspect_reservation_process(metadata)['status'] == 'gone')
    visible = {'value': False}
    f.monkeypatch.setattr(f.server, '_read_outcome_record', lambda r: actual_read(r) if visible['value'] else None)
    f.runtime.monitor_once()
    assert f.runtime.store.get(record['job_id'])['job_lifecycle'] not in {'succeeded', 'failed'}
    visible['value'] = True
    f.runtime.monitor_once()
    saved = f.runtime.store.get(record['job_id'])
    assert saved['job_lifecycle'] == 'succeeded', 'termination must not wait for the one-hour checking interval'
    assert saved['active_reservation'] is False
