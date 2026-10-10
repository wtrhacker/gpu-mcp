"""Fixture preparation contracts; human GPU experiments are never auto-started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from gpu_mcp_config import load_policy
from manual_shared_monitoring import current_case_job, observation_targets, prepare, stage_demo

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[1]


def create(tmp_path, **overrides):
    arguments = dict(runtime=ROOT, python=Path('/fixture/python'), nodes=['fixture-a'])
    arguments.update(overrides)
    return prepare(tmp_path / 'manual-repo', **arguments)


def test_preparation_is_hidden_neutral_and_does_not_launch_jobs(tmp_path):
    result = create(tmp_path)
    repo = Path(result['repo'])
    assert result['jobs_launched'] is False
    assert list((repo / '.gpu_mcp_state/jobs').iterdir()) == []
    plan = json.loads((repo / '.human-test/plan.json').read_text())
    assert plan['resource_selection'] == 'agent'
    assert plan['cases']['a']['delay_sec'] >= 140
    assert plan['cases']['b']['delay_sec'] >= 280
    assert plan['cases']['a']['exit_code'] == 17
    assert plan['cases']['b']['exit_code'] == 0
    for label in ('a', 'b'):
        prompt = (repo / '.human-test' / f'prompt-{label}.txt').read_text().lower()
        assert 'fail' not in prompt and 'sleep' not in prompt and 'report' not in prompt and 'poll' not in prompt
        assert 'select an available gpu' in prompt
        assert 'host' not in plan['cases'][label] and 'gpu_index' not in plan['cases'][label]
        public = json.loads((repo / 'work' / f'task_{label}.json').read_text())
        assert 'delay_sec' not in json.dumps(public) and 'exit_code' not in json.dumps(public)
        for stage in ('smoke', 'main'):
            script = (repo / public[stage]['script']).read_text()
            assert 'time.sleep(' in script and 'SystemExit(' in script
            assert 'torch' not in script and 'cuda' not in script
            assert 'GPU_MCP_PROGRESS ' in script
            assert public[stage]['job_role'] == stage


def test_fixture_policy_and_scoped_registration_are_valid(tmp_path):
    result = create(tmp_path)
    repo = Path(result['repo'])
    policy = load_policy(repo / 'gpu-mcp.toml')
    assert policy.nodes == ('fixture-a',)
    config = tomllib.loads((repo / '.codex/config.toml').read_text())
    rules = config['permissions']['manual-blind']['filesystem'][':workspace_roots']
    assert rules['jobs'] == rules['.human-test'] == rules['logs'] == rules['.gpu_mcp_state'] == 'deny'
    assert config['model'] == 'gpt-5.5' and config['model_reasoning_effort'] == 'medium'
    assert config['permissions']['manual-blind']['filesystem'][str(ROOT / 'test')] == 'deny'
    assert str(ROOT / 'gpu_mcp_codex_launcher.py') in config['mcp_servers']['gpu-cluster-mcp']['args']
    assert config['mcp_servers']['gpu-cluster-mcp']['tools']['reload_policy']['approval_mode'] == 'prompt'


def test_invalid_hosts_and_existing_fixture_are_refused(tmp_path):
    with pytest.raises(ValueError, match='approved host names'):
        create(tmp_path, nodes=[])
    create(tmp_path)
    with pytest.raises(ValueError, match='not overwritten'):
        create(tmp_path)


def test_observer_ignores_previous_round_and_status_update_order():
    old = {'job_id': 'old', 'script_name': 'experiment_b.py', 'created_at': '2026-10-08T21:00:00Z'}
    new = dict(old, job_id='new', created_at='2026-10-08T22:00:00Z')
    boundary = '2026-10-08T21:30:00+00:00'
    assert current_case_job([old], 'experiment_b.py', boundary) is None
    for jobs in ([old, new], [new, old]):
        assert current_case_job(jobs, 'experiment_b.py', boundary) == new


def execute_stage(repo, label, stage):
    # Run the actual approved-script guard, replacing only wall-clock waiting.
    driver = '''
import json, sys, time
from pathlib import Path
from gpu_mcp_guard import run_job
repo = Path(sys.argv[1])
spec = json.loads((repo / 'work' / ('task_' + sys.argv[2] + '.json')).read_text())[sys.argv[3]]
time.sleep = lambda delay: None
time.monotonic = lambda: 0
raise SystemExit(run_job(spec['script'], spec['args'], repo_root=repo,
                         script_roots=[repo / 'jobs'], write_roots=[repo / 'results']))
'''
    return subprocess.run([sys.executable, '-B', '-c', driver, str(repo), label, stage],
                          cwd=repo, capture_output=True, text=True,
                          env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1'))


@pytest.mark.parametrize('label', ['a', 'b'])
def test_guarded_stages_publish_demo_progress_and_expected_artifacts(tmp_path, label):
    result = create(tmp_path)
    repo = Path(result['repo'])
    plan = json.loads((repo / '.human-test/plan.json').read_text())['cases'][label]
    spec = json.loads((repo / 'work' / f'task_{label}.json').read_text())
    smoke = execute_stage(repo, label, 'smoke')
    assert smoke.returncode == 0, smoke.stderr
    readiness = json.loads((repo / spec['smoke']['output']).read_text())
    assert readiness['ready'] is True and readiness['simulated'] is True
    main = execute_stage(repo, label, 'main')
    assert main.returncode == plan['exit_code'], main.stderr
    reports = [json.loads(line.removeprefix('GPU_MCP_PROGRESS ')) for line in main.stdout.splitlines()
               if line.startswith('GPU_MCP_PROGRESS ')]
    assert reports[0]['current'] == 0
    assert reports[-1]['current'] == plan['stages']['main']['completed_steps']
    assert reports[-1]['total'] == plan['stages']['main']['total_steps']
    assert all('simulated' in report['message'] for report in reports)
    directory = repo / plan['result_dir']
    assert len((directory / 'main_samples.csv').read_text().splitlines()) == reports[-1]['current'] + 1
    assert (directory / 'summary.json').exists() == (label == 'b')
    if label == 'b':
        summary = json.loads((directory / 'summary.json').read_text())
        assert summary['completed_steps'] == summary['total_steps'] == 42
    else:
        assert reports[-1]['current'] < reports[-1]['total']


def test_main_needs_its_matching_smoke_output(tmp_path):
    repo = Path(create(tmp_path)['repo'])
    assert execute_stage(repo, 'a', 'main').returncode != 0
    assert execute_stage(repo, 'a', 'smoke').returncode == 0
    spec = json.loads((repo / 'work/task_a.json').read_text())
    output = repo / spec['smoke']['output']
    readiness = json.loads(output.read_text())
    readiness['parameters']['seed'] += 1
    output.write_text(json.dumps(readiness))
    assert execute_stage(repo, 'a', 'main').returncode != 0


def test_refresh_retains_policy_registration_and_previous_outputs(tmp_path):
    repo = Path(create(tmp_path)['repo'])
    policy, registration = (repo / 'gpu-mcp.toml').read_bytes(), (repo / '.codex/config.toml').read_bytes()
    old_plan = json.loads((repo / '.human-test/plan.json').read_text())
    old_output = repo / old_plan['cases']['a']['result_dir'] / 'retained.txt'
    old_output.write_text('previous run')
    new_plan = stage_demo(repo, old_plan)
    assert new_plan['round'] == old_plan['round'] + 1
    assert new_plan['cases']['a']['result_dir'] != old_plan['cases']['a']['result_dir']
    assert old_output.read_text() == 'previous run'
    assert (repo / 'gpu-mcp.toml').read_bytes() == policy
    assert (repo / '.codex/config.toml').read_bytes() == registration


def test_observer_tracks_both_stages_in_each_conversation(tmp_path):
    repo = Path(create(tmp_path)['repo'])
    plan = json.loads((repo / '.human-test/plan.json').read_text())
    assert {name: expected['script_name'] for name, expected in observation_targets(plan)} == {
        'a_smoke': 'smoke_a.py', 'a_main': 'experiment_a.py',
        'b_smoke': 'smoke_b.py', 'b_main': 'experiment_b.py'}


def test_preparation_creates_directory_targets_before_codex_denies_them(tmp_path):
    repo = Path(create(tmp_path)['repo'])
    assert (repo / '.gpu_mcp_state').is_dir()
    assert (repo / '.gpu_mcp_state/jobs').is_dir()
    assert list((repo / '.gpu_mcp_state/jobs').iterdir()) == []


def test_new_round_recreates_empty_state_directories_after_cleanup(tmp_path):
    import shutil
    repo = Path(create(tmp_path)['repo'])
    state = repo / '.gpu_mcp_state'
    if state.exists():
        shutil.rmtree(state)
    plan = json.loads((repo / '.human-test/plan.json').read_text())
    stage_demo(repo, plan)
    assert (state / 'jobs').is_dir()
