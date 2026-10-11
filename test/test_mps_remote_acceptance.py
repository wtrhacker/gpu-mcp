"""Opt-in CUDA/MPS acceptance. Uses real leases and the real guarded runner."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shlex
import time

import pytest

from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy
from test_gpu_mcp_server_contract import _import_server_with_config

pytestmark = pytest.mark.skipif(os.environ.get('GPU_MCP_RUN_MPS_REMOTE_TESTS') != '1',
                               reason='explicit CUDA/MPS acceptance opt-in required')
ROOT = Path(__file__).resolve().parents[1]


def wait(check, seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.25)
    raise AssertionError('CUDA/MPS acceptance condition timed out')


def test_human_demo_smoke_runs_real_cuda_and_keeps_case_diagnostics(monkeypatch):
    host = os.environ['GPU_MCP_MPS_TEST_HOST']
    root = ROOT / 'test_mcp_repos' / ('gpu-mcp-mps-demo-' + secrets.token_hex(5))
    repo = root / 'repo'
    for name in ('jobs', 'results', 'logs'):
        (repo / name).mkdir(parents=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=[{host!r}]\n"
                      "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\n")
    approval = root / 'approved.json'
    approve_policy(load_policy(config), store_path=approval, approved_by='pytest',
                   diff_summary=['isolated human demo smoke validation'])
    for name in ('GPU_MCP_TEST_RESERVATION_ROOT', 'GPU_MCP_TEST_DISABLE_POLICY_APPROVAL',
                 'GPU_MCP_TEST_MPS_DRIVER', 'GPU_MCP_CODEX_SHARED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GPU_MCP_TEST_POLICY_APPROVAL_STORE', str(approval))
    monkeypatch.setenv('GPU_MCP_TEST_MPS_CONTROL_ROOT', str(root / 'control'))
    server = _import_server_with_config(monkeypatch, config)
    (repo / 'jobs/demo.py').write_bytes((ROOT / 'test/fixtures/mps_demo_case.py').read_bytes())
    entries = [{'case_id': name, 'script_path': 'jobs/demo.py',
                'args': ['--phase', 'smoke', '--case', name, '--seconds', '8', '--output-dir', 'results/' + name],
                'output_file': 'logs/' + name + '.log'} for name in ('smoke-one', 'smoke-two')]
    job = None
    try:
        inventory = json.loads(server.check_gpus(samples=1))
        available = [g for g in inventory.get('gpus', []) if g.get('availability') == 'available']
        assert available, inventory
        selected = available[0]
        launched = json.loads(server.run_python_on_gpu(host=host, gpu_index=selected['gpu_index'],
            mps=True, max_concurrent=2, runs=entries, job_role='smoke', cadence_hint_sec=1,
            job_title='Human MPS demo smoke validation'))
        job = launched.get('job_id')
        assert launched['status'] == 'launched', launched
        def completed():
            value = json.loads(server.manage_gpu_job(job_id=job, action='status', detail='full',
                early_poll_reason='validate the repaired demo under the real guard and CUDA'))
            return value if value.get('job_lifecycle') in {'succeeded', 'failed'} else None
        final = wait(completed)
        assert final['job_lifecycle'] == 'succeeded' and not final['active_reservation'], final
        record = server._mps_manager().read(job)
        for entry in entries:
            case_id = entry['case_id']
            result = json.loads((repo / 'results' / case_id / 'summary.json').read_text())
            assert result['passed'] and result['value'] == 64 and result['attempt'] == 1
            # Inspect the source host directly: this must work even before an
            # NFS client exposes a remote-created log on the control machine.
            case = next(c for c in record['cases'] if c['case_id'] == case_id)
            log = server._mps_manager().rpc('logs', record['mps_spec'], request={
                'case_id': case_id, 'case_attempt_id': case['attempt_id'], 'lines': 80})
            assert case_id + ' completed:' in log['tail']
        (root / 'acceptance.json').write_text(json.dumps({'job_id': job, 'final': final, 'cases': record['cases']}, indent=2))
        print('Human MPS demo smoke evidence:', root)
    finally:
        if job:
            saved = server._mps_manager().read(job)
            if server._mps_manager().rpc('status', saved['mps_spec'])['state'] != 'gone':
                server.manage_gpu_job(job_id=job, action='stop')
                wait(lambda: server._mps_manager().rpc('status', saved['mps_spec'])['state'] == 'gone')
        server.HEARTBEAT_MANAGER.stop()


@pytest.mark.parametrize('framework', ['torch', 'openmm'])
def test_cuda_failure_stop_retry_preserve_sibling_progress(monkeypatch, framework):
    host = os.environ['GPU_MCP_MPS_TEST_HOST']
    gpu = int(os.environ.get('GPU_MCP_MPS_TEST_GPU', '0'))
    root = ROOT / 'test_mcp_repos' / ('gpu-mcp-mps-' + secrets.token_hex(5))
    repo = root / 'repo'
    for name in ('jobs', 'results', 'logs'):
        (repo / name).mkdir(parents=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=[{host!r}]\n"
                      "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\n")
    approval = root / 'approved.json'
    approve_policy(load_policy(config), store_path=approval, approved_by='pytest',
                   diff_summary=['test-owned MPS acceptance fixture'])
    for name in ('GPU_MCP_TEST_RESERVATION_ROOT', 'GPU_MCP_TEST_DISABLE_POLICY_APPROVAL',
                 'GPU_MCP_TEST_MPS_DRIVER', 'GPU_MCP_CODEX_SHARED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GPU_MCP_TEST_POLICY_APPROVAL_STORE', str(approval))
    monkeypatch.setenv('GPU_MCP_TEST_MPS_CONTROL_ROOT', str(root / 'control'))
    server = _import_server_with_config(monkeypatch, config)
    source = '''import json, sys, time
from pathlib import Path
import torch
duration, code, output = float(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
x = torch.ones((128,128), device='cuda')
started, steps = time.monotonic(), 0
while time.monotonic() - started < duration:
    y = x @ x
    torch.cuda.synchronize()
    assert float(y[0,0]) == 128
    steps += 1
    output.write_text(json.dumps({'steps':steps,'elapsed':time.monotonic()-started}))
    time.sleep(.15)
raise SystemExit(code)
'''
    if framework == 'openmm':
        source = source.replace('import torch', 'import openmm')
        source = source.replace("x = torch.ones((128,128), device='cuda')", '''system = openmm.System()
system.addParticle(1)
force = openmm.CustomExternalForce('x*x+y*y+z*z')
force.addParticle(0, [])
system.addForce(force)
integrator = openmm.VerletIntegrator(0.001)
context = openmm.Context(system, integrator, openmm.Platform.getPlatformByName('CUDA'), {'DeviceIndex':'0'})
context.setPositions([[0.1,0.2,0.3]])''')
        source = source.replace('    y = x @ x\n    torch.cuda.synchronize()\n    assert float(y[0,0]) == 128',
                                '    integrator.step(2)\n    state = context.getState(getEnergy=True)\n    assert state.getPotentialEnergy()._value >= 0')
    (repo / 'jobs/case.py').write_text(source)
    entries = [{'case_id': name, 'script_path': 'jobs/case.py',
                'args': [str(duration), str(code), f'results/{name}.json'],
                'output_file': f'logs/{name}.log'}
               for name, duration, code in [('bad',1,17), ('stop',30,0), ('keep',15,0), ('queued',1,0)]]
    job = None
    try:
        inventory = json.loads(server.check_gpus(samples=1))
        available = [r['gpu_index'] for r in inventory.get('gpus', []) if r.get('availability') == 'available']
        assert available, inventory
        if gpu not in available:
            gpu = available[0]
        launched = json.loads(server.run_python_on_gpu(host=host, gpu_index=gpu, mps=True,
            runs=entries, max_concurrent=3, cadence_hint_sec=1, job_role='one_off', job_title='Real MPS acceptance'))
        job = launched.get('job_id')
        assert launched['status'] == 'launched', launched
        def status():
            result = json.loads(server.manage_gpu_job(job_id=job, action='status',
                detail='full', early_poll_reason='verify CUDA/MPS acceptance'))
            assert result['status'] == 'ok', result
            return result
        def cases():
            status()
            return {c['case_id']: c for c in server._mps_manager().read(job)['cases']}
        wait(lambda: cases().get('bad', {}).get('state') == 'failed')
        original = cases()['bad']
        assert cases()['keep']['state'] == 'running'
        captured = server._mps_manager().read(job)['mps_spec']
        service = server._mps_manager().rpc('status', captured)
        probe = {'binary':captured['mps_binary'], 'pipe':captured['pipe_directory'],
                 'gpu_uuid':captured['gpu_uuid'], 'pid':service['server']['pid']}
        code = ('import json,os,subprocess,sys;p=json.loads(sys.argv[1]);'
                'e=dict(os.environ,CUDA_MPS_PIPE_DIRECTORY=p["pipe"],CUDA_VISIBLE_DEVICES=p["gpu_uuid"],CUDA_MPS_PROTOCOL_VERSION="2");'
                'r=subprocess.run([p["binary"]],input="get_server_status "+str(p["pid"])+"\\n",env=e,capture_output=True,text=True,timeout=5);'
                'print(r.stdout.strip())')
        controller_status = server._host_run(host, shlex.join([server.PYTHON,'-I','-c',code,json.dumps(probe)]), timeout=10)
        assert controller_status == 'ACTIVE', controller_status
        progress = json.loads((repo / 'results/keep.json').read_text())['steps']
        stopped = json.loads(server.manage_gpu_job(job_id=job, case_id='stop', action='stop'))
        assert stopped['status'] == 'stop_queued', stopped
        wait(lambda: cases()['stop']['state'] == 'cancelled')
        # An explicit source change makes the retry succeed; old logs remain.
        (repo / 'jobs/case.py').write_text(source.replace('raise SystemExit(code)', 'raise SystemExit(0)'))
        retry = json.loads(server.manage_gpu_job(job_id=job, case_id='bad', action='retry'))
        assert retry['status'] == 'retry_queued', retry
        wait(lambda: cases()['bad']['state'] == 'succeeded')
        after = cases()
        assert after['bad']['attempt_id'] != original['attempt_id']
        assert len(after['bad']['attempts']) == 2
        assert json.loads((repo / 'results/keep.json').read_text())['steps'] > progress
        final = wait(lambda: status() if status()['job_lifecycle'] == 'failed' and not status()['active_reservation'] else None)
        assert cases()['keep']['state'] == 'succeeded'
        assert cases()['queued']['state'] == 'succeeded'
        observed = server._mps_manager().rpc('status', server._mps_manager().read(job)['mps_spec'])
        assert observed['state'] == 'gone' and not observed['live_processes']
        assert {r['case_id'] for r in observed['termination_receipts']} == {'bad','stop','keep','queued'}
        (root / 'acceptance.json').write_text(json.dumps({'host':host,'gpu':gpu,'protocol':2,'framework':framework,
            'version':server._mps_manager().capability(host,2), 'gpu_uuid':server._mps_manager().read(job)['mps_spec']['gpu_uuid'],
            'controller_status':controller_status, 'final':final,'state':observed}, indent=2))
        print('MPS acceptance artifacts:', root)
    finally:
        if job:
            record = server._mps_manager().read(job)
            observed = server._mps_manager().rpc('status', record['mps_spec'])
            if observed['state'] != 'gone':
                server.manage_gpu_job(job_id=job, action='stop')
                wait(lambda: server._mps_manager().rpc('status', record['mps_spec'])['state'] == 'gone')
            server.manage_gpu_job(job_id=job, action='finish')
        server.HEARTBEAT_MANAGER.stop()
