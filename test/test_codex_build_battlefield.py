"""Short, opt-in live Codex tests for separate compilation and execution."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

import pytest

from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy

ROOT = Path(__file__).resolve().parents[1]
LIVE = pytest.mark.skipif(os.environ.get('GPU_MCP_RUN_BUILD_BATTLEFIELD_TESTS') != '1',
                          reason='set GPU_MCP_RUN_BUILD_BATTLEFIELD_TESTS=1 for live Codex build tests')
REMOTE = pytest.mark.skipif(os.environ.get('GPU_MCP_BUILD_TEST_HOST') is None,
                            reason='set a verified GPU_MCP_BUILD_TEST_HOST for remote tests')
pytestmark = pytest.mark.codex_exec


def fixture_repo(name, host):
    root = ROOT / 'test_mcp_repos' / f'gpu-mcp-build-battlefield-{name}-{secrets.token_hex(4)}'
    repo = root / 'repo'
    for directory in ('jobs', 'results', '.gpu_mcp_logs', '.codex'):
        (repo / directory).mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=[{host!r}]\n"
                      "script_roots=['jobs']\nwrite_roots=['.']\noutput_roots=['.gpu_mcp_logs']\n"
                      'build_enabled=true\nsync_timeout_sec=60\n')
    store = root / 'approved-policies.json'
    approve_policy(load_policy(config), store_path=store, approved_by='pytest',
                   diff_summary=['test-owned artifact build fixture'])
    env = dict(os.environ, PYTEST_CURRENT_TEST='gpu-mcp-build-battlefield',
               GPU_MCP_TEST_POLICY_APPROVAL_STORE=str(store),
               GPU_MCP_TEST_BUILD_CONTROL_ROOT=str(root / 'control'),
               GPU_MCP_TEST_BUILD_WORKER_ROOT=str(root / 'workers'))
    # Remote GPU tests use the normal shared reservation registry.
    env.pop('GPU_MCP_TEST_RESERVATION_ROOT', None)
    env.pop('GPU_MCP_TEST_DISABLE_POLICY_APPROVAL', None)
    lines = ['[features]', 'hooks = true', '[mcp_servers.gpu-cluster-mcp]',
             f'command = {sys.executable!r}',
             f'args = [{str(ROOT / "gpu_mcp_server.py")!r}, "--config", {str(config)!r}]',
             'startup_timeout_sec = 20', 'tool_timeout_sec = 60',
             '[mcp_servers.gpu-cluster-mcp.env]']
    for key in ('PYTEST_CURRENT_TEST', 'GPU_MCP_TEST_POLICY_APPROVAL_STORE',
                'GPU_MCP_TEST_BUILD_CONTROL_ROOT', 'GPU_MCP_TEST_BUILD_WORKER_ROOT'):
        lines.append(f'{key} = {env[key]!r}')
    for tool in ('build_python_artifact', 'manage_gpu_job', 'run_python_on_gpu', 'check_gpus'):
        lines += [f'[mcp_servers.gpu-cluster-mcp.tools.{tool}]', 'approval_mode = "approve"']
    (repo / '.codex/config.toml').write_text('\n'.join(lines) + '\n')
    return root, repo, env


def run_codex(root, repo, env, prompt, timeout=180):
    output = root / 'codex-result.txt'
    command = ['codex', '-m', 'gpt-5.5', '-c', 'model_reasoning_effort="medium"',
               '--ask-for-approval', 'never', 'exec', '-C', str(repo),
               '--sandbox', 'workspace-write', '--dangerously-bypass-hook-trust',
               '--ephemeral', '--skip-git-repo-check', '--output-last-message', str(output),
               prompt]
    result = subprocess.run(command, env=env, cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    (root / 'codex-transcript.txt').write_text(result.stdout)
    assert result.returncode == 0, result.stdout[-8000:]
    records = [json.loads(path.read_text()) for path in (root / 'control').glob('job-*/job.json')]
    assert records, result.stdout[-8000:]
    assert all(record['job_lifecycle'] in {'succeeded', 'failed', 'finished'} for record in records), records
    return records


@LIVE
def test_codex_builds_cpu_artifact_without_gpu_checks():
    root, repo, env = fixture_repo('cpu', 'localhost')
    (repo / 'jobs/build.py').write_text('''
import os, subprocess
from pathlib import Path
assert not list(Path('/dev').glob('nvidia*'))
subprocess.run(['cc','-shared','-fPIC','-x','c','-','-o',os.environ['GPU_MCP_ARTIFACT_PATH']],
    input='int answer(void) { return 42; }',text=True,check=True)
Path('results/gpu-access.txt').write_text('denied')
''')
    records = run_codex(root, repo, env,
        'Do not use shell commands or edit files. Use gpu-cluster-mcp/build_python_artifact '
        'with host="localhost", script_path="jobs/build.py", artifact_path="results/answer.so", '
        'build_mode="cpu_only", output_file=".gpu_mcp_logs/build.log", cadence_hint_sec=1. '
        'Do not inspect or reserve a GPU for this CPU-only build. '
        'Reconcile its terminal result with manage_gpu_job(action="status", job_id=the returned ID). '
        'An early_poll_reason="short compiler smoke" is appropriate if needed. '
        'Report the build mode, final status, and artifact path briefly.')
    assert len(records) == 1 and records[0]['job_lifecycle'] == 'succeeded'
    assert records[0]['reservation_key'] is None and records[0]['gpu_index'] is None
    assert (repo / 'results/answer.so').exists()
    transcript = (root / 'codex-transcript.txt').read_text()
    assert 'check_gpus({' not in transcript


@LIVE
@REMOTE
def test_codex_cpu_cuda_build_then_normal_gpu_execution():
    host = os.environ['GPU_MCP_BUILD_TEST_HOST']
    gpu = int(os.environ.get('GPU_MCP_BUILD_TEST_GPU', '1'))
    root, repo, env = fixture_repo('cuda', host)
    for name in ('build_cuda_probe.py', 'run_cuda_probe.py'):
        shutil.copy2(ROOT / 'test/build_jobs' / name, repo / 'jobs' / name)
    records = run_codex(root, repo, env,
        f'Do not use shell commands or edit files. First call build_python_artifact with host={host!r}, '
        'script_path="jobs/build_cuda_probe.py", args=["compute_89"], '
        'artifact_path="results/build_cuda_probe.so", build_mode="cpu_only", '
        'output_file=".gpu_mcp_logs/build.log", cadence_hint_sec=1. '
        'Reconcile its terminal status. This CPU build needs no GPU inspection. '
        f'If it succeeds, inspect availability and run run_python_on_gpu on host={host!r}, gpu_index={gpu}, '
        'script_path="jobs/run_cuda_probe.py", args=["results/build_cuda_probe.so","results/check.json"], '
        'output_file=".gpu_mcp_logs/run.log", job_role="one_off", expected_duration_sec=10, cadence_hint_sec=5. '
        'Reconcile that terminal status too. Use an early_poll_reason for these short smoke checks if needed. '
        'Stop on a failed build; do not change policies or work around a refusal. Report briefly.', timeout=240)
    assert records[0]['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/build.log').read_text()
    check = json.loads((repo / 'results/check.json').read_text())
    assert check['passed']


@LIVE
@REMOTE
def test_codex_torch_artifact_build_and_guarded_load():
    host = os.environ['GPU_MCP_BUILD_TEST_HOST']
    gpu = int(os.environ.get('GPU_MCP_BUILD_TEST_GPU', '1'))
    root, repo, env = fixture_repo('torch', host)
    for name in ('build_torch_force.py', 'run_torch_force.py'):
        shutil.copy2(ROOT / 'test/build_jobs' / name, repo / 'jobs' / name)
    records = run_codex(root, repo, env,
        f'Do not use shell commands or edit files. Call build_python_artifact with host={host!r}, '
        f'build_mode="gpu_assisted", gpu_index={gpu}, script_path="jobs/build_torch_force.py", '
        'artifact_path="results/pair.pt2", output_file=".gpu_mcp_logs/build.log", '
        'expected_duration_sec=30, cadence_hint_sec=15. Reconcile the terminal status. '
        'If successful, run the existing run_python_on_gpu on the same host and GPU with '
        'script_path="jobs/run_torch_force.py", args=["results/pair.pt2","results/check.json"], '
        'output_file=".gpu_mcp_logs/run.log", job_role="one_off", expected_duration_sec=10, cadence_hint_sec=5. '
        'Reconcile the terminal run status. For these short smoke checks an early_poll_reason is appropriate '
        'if needed. Stop on a failed build; do not change policies or bypass a refusal. Report briefly.', timeout=240)
    assert records[0]['job_lifecycle'] == 'succeeded', (repo / '.gpu_mcp_logs/build.log').read_text()
    assert not records[0].get('active_reservation')
    check = json.loads((repo / 'results/check.json').read_text())
    assert check['passed'] and check['phases'] == 3
