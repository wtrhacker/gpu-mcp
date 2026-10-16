"""Opt-in acceptance at the actual manual fixture / native sandbox boundary.

No model is started. Two real guarded smoke scripts use private bookkeeping;
the native sandbox simultaneously enforces the fixture's blind read policy.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from gpu_mcp_codex_runtime import CodexRuntime
from gpu_mcp_codex_state import UNCERTAIN_STATES
from manual_shared_monitoring import prepare
from test_gpu_mcp_codex_shared import QuietDaemon

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.real_battlefield, pytest.mark.skipif(
    os.environ.get('GPU_MCP_RUN_MANUAL_SANDBOX_TESTS') != '1', reason='opt-in native manual-fixture acceptance')]


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml_value(v) for k, v in value.items()) + '}'
    return json.dumps(value)


def test_prepared_blind_fixture_runs_both_smokes_while_native_commands_are_sandboxed(tmp_path, monkeypatch):
    import tomllib
    repo = tmp_path / 'repo'
    prepare(repo, runtime=ROOT, python=Path(sys.executable), nodes=['localhost'])
    registration = tomllib.loads((repo / '.codex/config.toml').read_text())
    profile = registration['permissions']['manual-blind']
    binary = os.environ.get('GPU_MCP_TEST_CODEX_BINARY') or shutil.which('codex')
    assert binary, 'Codex CLI is required for native fixture acceptance'
    sandbox = subprocess.Popen([binary, 'sandbox', '-C', str(repo), '-P', 'manual-blind',
        '-c', 'permissions.manual-blind=' + toml_value(profile), '--', sys.executable, '-B', '-c',
        'import json,time;from pathlib import Path;'
        'json.loads(Path("work/task_a.json").read_text());'
        'print("READY",flush=True);time.sleep(8)'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    runtime = server = None
    try:
        assert select.select([sandbox.stdout], [], [], 10)[0], 'native sandbox did not start'
        assert sandbox.stdout.readline().strip() == 'READY', sandbox.stderr.read() if sandbox.poll() is not None else ''
        # This is the exact missing assertion in the previous preparation tests.
        assert (repo / '.gpu_mcp_state/jobs').is_dir(), 'sandbox preparation obstructed the MCP state directory'
        monkeypatch.setenv('GPU_MCP_TEST_RESERVATION_ROOT', str(tmp_path / 'leases'))
        monkeypatch.setenv('GPU_MCP_PYTHON', sys.executable)
        monkeypatch.setattr(sys, 'argv', ['gpu_mcp_backend.py', '--config', str(repo / 'gpu-mcp.toml')])
        name = 'native_manual_acceptance_' + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, ROOT / 'gpu_mcp_backend.py')
        server = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, server)
        spec.loader.exec_module(server)
        runtime = CodexRuntime(server, tmp_path / 'authority', daemon=QuietDaemon())
        server.CODEX_RUNTIME = runtime
        actor = {'threadId': str(uuid.uuid4())}
        monkeypatch.setattr(server.mcp, 'get_context', lambda: SimpleNamespace(
            request_context=SimpleNamespace(meta=dict(actor))))
        monkeypatch.setattr(server, '_process_gpu_usage', lambda *args, **kwargs: (None, None))
        jobs = []
        for gpu, label in enumerate(('a', 'b')):
            actor['threadId'] = str(uuid.uuid4())
            task = json.loads((repo / 'work' / ('task_' + label + '.json')).read_text())['smoke']
            reply = json.loads(server.run_python_on_gpu(host='localhost', gpu_index=gpu,
                script_path=task['script'], args=task['args'], job_role='smoke', cadence_hint_sec=1))
            assert reply['status'] in {'launched', 'launched_with_warning'}, reply
            jobs.append((reply['job_id'], actor['threadId']))
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            runtime.monitor_once()
            runtime.deliver_once()
            if all(runtime.store.get(job)['job_lifecycle'] == 'succeeded'
                   and not runtime.store.get(job)['active_reservation'] for job, _ in jobs):
                break
            time.sleep(.2)
        for job, owner in jobs:
            saved = runtime.store.get(job)
            assert saved['job_lifecycle'] == 'succeeded', saved
            assert saved['active_reservation'] is False
            sent = [e for e in runtime.daemon.sent if e['job_id'] == job and e['kind'] != 'poll']
            assert len(sent) == 1 and sent[0]['kind'] == 'succeeded' and sent[0]['origin'] == owner
            assert not any(e['kind'] in UNCERTAIN_STATES for e in runtime.store.events() if e['job_id'] == job)
        assert sandbox.wait(timeout=10) == 0
        assert (repo / '.gpu_mcp_state/jobs').is_dir()
        print('Both prepared smoke jobs succeeded under the actual native blind permission profile.')
    finally:
        if sandbox.poll() is None:
            sandbox.terminate()
            sandbox.wait(timeout=5)
        if runtime is not None:
            runtime.stop()
        if server is not None:
            server.HEARTBEAT_MANAGER.stop()
