"""Direct server acceptance for compiler debugging; live Codex coverage is separate."""
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from test_codex_build_battlefield import fixture_repo, ROOT
from test_gpu_mcp_server_contract import _import_server_with_config

pytestmark = pytest.mark.skipif(os.environ.get('GPU_MCP_RUN_BUILD_REMOTE_TESTS') != '1',
                               reason='explicit real build acceptance opt-in required')


def test_cpu_build_environment_probe(monkeypatch):
    host = os.environ['GPU_MCP_BUILD_TEST_HOST']
    root, repo, env = fixture_repo('environment', host)
    for key in ('PYTEST_CURRENT_TEST', 'GPU_MCP_TEST_POLICY_APPROVAL_STORE',
                'GPU_MCP_TEST_BUILD_CONTROL_ROOT', 'GPU_MCP_TEST_BUILD_WORKER_ROOT'):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv('GPU_MCP_TEST_RESERVATION_ROOT', raising=False)
    monkeypatch.delenv('GPU_MCP_TEST_DISABLE_POLICY_APPROVAL', raising=False)
    (repo / 'jobs/environment.py').write_text('''
import importlib.metadata, json, os, sys
from pathlib import Path
versions = {}
for name in ('torch', 'openmm'):
    try:
        versions[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        versions[name] = None
Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text(json.dumps({'python': sys.version, 'versions': versions}))
''')
    server = _import_server_with_config(monkeypatch, repo / 'gpu-mcp.toml')
    try:
        result = terminal(server, json.loads(server.build_python_artifact(
            host=host, script_path='jobs/environment.py', artifact_path='results/environment.json',
            output_file='.gpu_mcp_logs/environment.log', build_mode='cpu_only', cadence_hint_sec=1)))
        assert result['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/environment.log').read_text()
        print('Build environment:', (repo / 'results/environment.json').read_text())
    finally:
        server.HEARTBEAT_MANAGER.stop()


def terminal(server, launch, seconds=150):
    assert launch['status'] in {'launched', 'retried'}, launch
    deadline = time.monotonic() + seconds
    result = launch
    while time.monotonic() < deadline:
        time.sleep(1)
        result = json.loads(server.manage_gpu_job(action='status', job_id=launch['job_id'],
                                                 early_poll_reason='contained compiler acceptance'))
        if result.get('job_lifecycle') in {'succeeded', 'failed'}:
            return result
    server.manage_gpu_job(action='stop', job_id=launch['job_id'])
    raise AssertionError(f'Build acceptance timed out: {result}')


def test_remote_torch_build_load_and_optional_openmm(monkeypatch):
    host = os.environ['GPU_MCP_BUILD_TEST_HOST']
    gpu = int(os.environ.get('GPU_MCP_BUILD_TEST_GPU', '1'))
    root, repo, env = fixture_repo('torch-acceptance', host)
    for key in ('PYTEST_CURRENT_TEST', 'GPU_MCP_TEST_POLICY_APPROVAL_STORE',
                'GPU_MCP_TEST_BUILD_CONTROL_ROOT', 'GPU_MCP_TEST_BUILD_WORKER_ROOT'):
        monkeypatch.setenv(key, env[key])
    monkeypatch.delenv('GPU_MCP_TEST_RESERVATION_ROOT', raising=False)
    monkeypatch.delenv('GPU_MCP_TEST_DISABLE_POLICY_APPROVAL', raising=False)
    for name in ('build_torch_force.py', 'run_torch_force.py'):
        shutil.copy2(ROOT / 'test/build_jobs' / name, repo / 'jobs' / name)
    server = _import_server_with_config(monkeypatch, repo / 'gpu-mcp.toml')
    try:
        inventory = server._host_run(host, 'nvidia-smi --query-gpu=index,uuid,memory.used --format=csv,noheader', timeout=10)
        processes = server._host_run(host, 'nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader', timeout=10)
        print('GPU inventory:', inventory)
        print('Compute processes:', processes)
        built = terminal(server, json.loads(server.build_python_artifact(
            host=host, script_path='jobs/build_torch_force.py', artifact_path='results/pair.pt2',
            build_mode='gpu_assisted', gpu_index=gpu, output_file='.gpu_mcp_logs/build.log', cadence_hint_sec=15)))
        assert built['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/build.log').read_text()[-7000:]
        loaded = terminal(server, json.loads(server.run_python_on_gpu(
            host=host, gpu_index=gpu, script_path='jobs/run_torch_force.py',
            args=['results/pair.pt2', 'results/check.json'], output_file='.gpu_mcp_logs/run.log',
            job_role='one_off', expected_duration_sec=10, cadence_hint_sec=5)))
        assert loaded['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/run.log').read_text()[-7000:]
        project = os.environ.get('GPU_MCP_BUILD_TEST_PROJECT_ROOT')
        if project:
            shutil.copy2(Path(project) / 'benchmarks/probe_aot_openmm.py', repo / 'jobs/probe_aot_openmm.py')
            coupled = terminal(server, json.loads(server.run_python_on_gpu(
                host=host, gpu_index=gpu, script_path='jobs/probe_aot_openmm.py',
                args=['--project-root', project, '--artifact', 'results/pair.pt2', '--output', 'results/openmm.json'],
                output_file='.gpu_mcp_logs/openmm.log', job_role='one_off', expected_duration_sec=10, cadence_hint_sec=5)))
            assert coupled['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/openmm.log').read_text()[-7000:]
        (root / 'acceptance.json').write_text(json.dumps({'build': built, 'load': loaded,
                                                       'project_probe': bool(project)}, indent=2))
        print('Acceptance artifacts:', root)
    finally:
        server.HEARTBEAT_MANAGER.stop()
