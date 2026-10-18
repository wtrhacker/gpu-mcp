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


@pytest.mark.parametrize('stop_group', [False, True], ids=['completion', 'parent-stop'])
def test_private_mps_coexists_with_ordinary_cuda_and_leaves_it_running(monkeypatch, stop_group):
    """Own the test card, then create an unrelated CUDA context before admission.

    Only the hardware-probe seam is wrapped to prepare that starting condition.
    Both admission decisions, the MPS service, guarded clients and cleanup are
    real. The ordinary process is test-owned and exits on a sentinel; production
    jobs and services are never used as fixtures.
    """
    from gpu_mcp_builds import BuildManager
    host = os.environ['GPU_MCP_MPS_TEST_HOST']
    preferred_gpu = int(os.environ.get('GPU_MCP_MPS_TEST_GPU', '0'))
    root = ROOT / 'test_mcp_repos' / ('gpu-mcp-mps-sharing-' + secrets.token_hex(5))
    repo = root / 'repo'
    for name in ('jobs', 'results', 'logs'):
        (repo / name).mkdir(parents=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=[{host!r}]\n"
                      "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\n")
    approval = root / 'approved.json'
    approve_policy(load_policy(config), store_path=approval, approved_by='pytest',
                   diff_summary=['isolated ordinary-CUDA and private-MPS coexistence test'])
    for name in ('GPU_MCP_TEST_RESERVATION_ROOT', 'GPU_MCP_TEST_DISABLE_POLICY_APPROVAL',
                 'GPU_MCP_TEST_MPS_DRIVER', 'GPU_MCP_CODEX_SHARED'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GPU_MCP_TEST_POLICY_APPROVAL_STORE', str(approval))
    monkeypatch.setenv('GPU_MCP_TEST_MPS_CONTROL_ROOT', str(root / 'control'))
    server = _import_server_with_config(monkeypatch, config)
    source = '''import json, sys, time
from pathlib import Path
import torch
output, stop = Path(sys.argv[1]), Path(sys.argv[2])
duration = float(sys.argv[3])
x = torch.ones((64, 64), device='cuda')
started, steps = time.monotonic(), 0
while time.monotonic() - started < duration and not stop.exists():
    y = x @ x
    torch.cuda.synchronize()
    assert float(y[0, 0]) == 64
    steps += 1
    output.write_text(json.dumps({'steps': steps, 'value': 64}))
    time.sleep(.1)
'''
    script = repo / 'jobs' / 'cuda_case.py'
    script.write_text(source)
    ordinary_output, ordinary_stop = repo / 'results' / 'ordinary.json', repo / 'results' / 'ordinary.stop'
    outside = {}
    job = None
    def remote(code, *args):
        command = shlex.join([server.PYTHON, '-B', '-I', '-c', code, *map(str, args)])
        raw = server._host_run(host, command, timeout=20)
        assert raw is not None, server._host_run_error(host, command)
        return json.loads(raw)
    def ordinary_snapshot():
        return remote('''import json,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gpu_mcp_mps_worker as worker
try:
    progress = json.loads(Path(sys.argv[3]).read_text())
except (OSError, ValueError):
    progress = {}
print(json.dumps({'alive': worker.same_process(json.loads(sys.argv[2])), **progress}))
''', ROOT, json.dumps(outside), ordinary_output)
    original_probe = BuildManager.probe_gpu
    def prepare_ordinary_process(manager, selected_host, selected_gpu, *, allow_existing_cuda=False):
        assert allow_existing_cuda and not outside
        # MpsManager acquired its normal reservation before calling this probe.
        # Verify a genuinely unused card before starting our small ordinary job.
        gpu_uuid, _ = original_probe(manager, selected_host, selected_gpu)
        outside.update(remote('''import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gpu_mcp_mps_worker as worker
env = dict(os.environ, CUDA_VISIBLE_DEVICES=sys.argv[2], CUDA_MPS_PIPE_DIRECTORY='')
with open(sys.argv[6], 'xb') as log:
    process = subprocess.Popen([sys.executable, '-B', sys.argv[3], sys.argv[4], sys.argv[5], '120'],
        env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
print(json.dumps(worker.identity(process.pid)))
''', ROOT, gpu_uuid, script, ordinary_output, ordinary_stop, repo / 'logs' / 'ordinary.log'))
        wait(lambda: ordinary_snapshot().get('steps', 0) >= 2)
        # Prove the opt-in changes admission for this exact real CUDA process.
        with pytest.raises(ValueError, match='already has a compute process'):
            original_probe(manager, selected_host, selected_gpu)
        return original_probe(manager, selected_host, selected_gpu, allow_existing_cuda=True)
    monkeypatch.setattr(BuildManager, 'probe_gpu', prepare_ordinary_process)
    try:
        inventory = json.loads(server.check_gpus(samples=1))
        available = [g['gpu_index'] for g in inventory.get('gpus', []) if g.get('availability') == 'available']
        assert available, inventory
        gpu = preferred_gpu if preferred_gpu in available else available[0]
        entries = [{'case_id': 'managed', 'script_path': 'jobs/cuda_case.py',
                    'args': ['results/managed.json', 'results/managed.stop', '30' if stop_group else '8'],
                    'output_file': 'logs/managed.log'}]
        launched = json.loads(server.run_python_on_gpu(host=host, gpu_index=gpu, mps=True,
            mps_allow_existing_cuda=True, runs=entries, max_concurrent=1, job_role='one_off',
            cadence_hint_sec=1, job_title='Private MPS coexistence acceptance'))
        job = launched.get('job_id')
        assert launched['status'] == 'launched' and launched['mps_allow_existing_cuda'], launched
        def status():
            result = json.loads(server.manage_gpu_job(job_id=job, action='status', detail='full',
                early_poll_reason='verify coexistence and preserve the independent CUDA process'))
            assert result['status'] == 'ok', result
            return result
        def running():
            status()
            cases = server._mps_manager().read(job)['cases']
            return cases and cases[0]['state'] == 'running'
        wait(running)
        captured = server._mps_manager().read(job)['mps_spec']
        service = server._mps_manager().rpc('status', captured)
        client_pids = remote(r'''import json,os,subprocess,sys
env=dict(os.environ, CUDA_MPS_PIPE_DIRECTORY=sys.argv[2], CUDA_MPS_PROTOCOL_VERSION='2')
result=subprocess.run([sys.argv[1]], input='get_client_list '+sys.argv[3]+'\n',
                      env=env, capture_output=True, text=True, check=True, timeout=5)
print(json.dumps([int(v) for v in result.stdout.split() if v.isdigit()]))
''', captured['mps_binary'], captured['pipe_directory'], service['server']['pid'])
        assert outside['pid'] not in client_pids
        assert service['cases'][0]['process']['pid'] in client_pids
        before = ordinary_snapshot()
        assert before['alive'] and before['steps'] > 0
        if stop_group:
            stopped = json.loads(server.manage_gpu_job(job_id=job, action='stop'))
            assert stopped['status'] == 'stop_queued', stopped
        def completed():
            value = status()
            return value if not value['active_reservation'] else None
        final = wait(completed)
        expected = 'failed' if stop_group else 'succeeded'
        assert final['job_lifecycle'] == expected and final['mps_allow_existing_cuda'], final
        observed = server._mps_manager().rpc('status', captured)
        assert observed['state'] == 'gone' and not observed['live_processes']
        at_cleanup = ordinary_snapshot()
        assert at_cleanup['alive']
        def progressed_after_cleanup():
            value = ordinary_snapshot()
            return value if value.get('alive') and value.get('steps', 0) > at_cleanup['steps'] else None
        after = wait(progressed_after_cleanup)
        assert outside['pid'] not in {receipt['process']['pid'] for receipt in observed['termination_receipts']}
        (root / 'acceptance.json').write_text(json.dumps({'host': host, 'gpu': gpu,
            'gpu_uuid': captured['gpu_uuid'], 'stop_group': stop_group, 'ordinary_process': outside,
            'ordinary_before_cleanup': before, 'ordinary_at_cleanup': at_cleanup,
            'ordinary_after_cleanup': after,
            'private_mps_client_pids': client_pids, 'final': final, 'state': observed}, indent=2))
        print('MPS coexistence acceptance evidence:', root)
    finally:
        try:
            if job:
                saved = server._mps_manager().read(job)
                if server._mps_manager().rpc('status', saved['mps_spec'])['state'] != 'gone':
                    server.manage_gpu_job(job_id=job, action='stop')
                    wait(lambda: server._mps_manager().rpc('status', saved['mps_spec'])['state'] == 'gone')
                server.manage_gpu_job(job_id=job, action='finish')
        finally:
            if outside:
                remote('from pathlib import Path; import json,sys; Path(sys.argv[1]).touch(); print(json.dumps(True))', ordinary_stop)
                wait(lambda: not ordinary_snapshot()['alive'], seconds=20)
            server.HEARTBEAT_MANAGER.stop()
