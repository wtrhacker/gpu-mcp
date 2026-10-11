import importlib.util
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

from manual_mps import prepare
from gpu_mcp_guard import scan_python_script_safety


def test_preparation_preserves_registration_and_policy_and_previous_runs(tmp_path):
    (tmp_path / '.codex').mkdir()
    policy = tmp_path / 'gpu-mcp.toml'
    registration = tmp_path / '.codex/config.toml'
    policy.write_text('approved bytes stay unchanged\n')
    registration.write_text('blind access permissions stay unchanged\n')
    before = (policy.read_bytes(), registration.read_bytes())
    plan = prepare(tmp_path, run_id='first')
    manifest = json.loads((tmp_path / plan['task']).read_text())
    assert manifest['main']['max_concurrent'] == 2
    assert len(manifest['main']['runs']) == 4
    assert len({r['output_file'] for r in manifest['main']['runs']}) == 4
    script = tmp_path / manifest['main']['runs'][0]['script_path']
    assert scan_python_script_safety(str(script), repo_root=tmp_path, script_roots=[tmp_path / 'jobs']) == []
    assert (policy.read_bytes(), registration.read_bytes()) == before
    with pytest.raises(ValueError, match='already exists'):
        prepare(tmp_path, run_id='first')
    prepare(tmp_path, run_id='second')
    assert (tmp_path / plan['task']).is_file()


def test_demo_failure_requires_second_invocation_and_preserves_evidence(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('demo_case', Path(__file__).parent / 'fixtures/mps_demo_case.py')
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    class Matrix:
        def __matmul__(self, other):
            return self

        def __getitem__(self, index):
            return 64.0

    clock = [0.0]
    monkeypatch.setattr(demo.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(demo.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        ones=lambda *a, **kw: Matrix(), cuda=SimpleNamespace(synchronize=lambda: None)))
    args = ['--case', 'contrast', '--phase', 'main', '--seconds', '90', '--output-dir', str(tmp_path)]
    with pytest.raises(RuntimeError, match='Recoverable input staging'):
        demo.main(args)
    assert not (tmp_path / 'summary.json').exists()
    failure = (tmp_path / 'attempt-1/failure.json').read_bytes()
    assert json.loads(failure)['elapsed_sec'] == 45
    demo.main(args)
    assert (tmp_path / 'attempt-1/failure.json').read_bytes() == failure
    result = json.loads((tmp_path / 'summary.json').read_text())
    assert result['passed'] is True and result['value'] == 64 and result['attempt'] == 2
    assert (tmp_path / 'attempt-2/summary.json').is_file()


def test_demo_runs_under_the_actual_runtime_guard(tmp_path):
    jobs, output = tmp_path / 'jobs', tmp_path / 'results'
    jobs.mkdir(); output.mkdir()
    script = jobs / 'demo.py'
    script.write_bytes((Path(__file__).parent / 'fixtures/mps_demo_case.py').read_bytes())
    # Only CUDA and the clock are simulated. File writes pass through the same
    # irreversible audit hook used by real GPU and MPS case processes.
    driver = '''import sys,time
from pathlib import Path
from types import SimpleNamespace
from gpu_mcp_guard import run_job
class Matrix:
 def __matmul__(self, other): return self
 def __getitem__(self, index): return 64.0
clock=[0.0]
time.monotonic=lambda:clock[0]
def sleep(seconds): clock[0]+=seconds
time.sleep=sleep
sys.modules['torch']=SimpleNamespace(ones=lambda *a,**kw:Matrix(),cuda=SimpleNamespace(synchronize=lambda:None))
script,output,phase,case=sys.argv[1:]
repo=Path(script).parent.parent
run_job(script,['--case',case,'--phase',phase,'--seconds','90','--output-dir',output],
 repo_root=repo,script_roots=[repo/'jobs'],write_roots=[repo/'results'])
'''
    def run(phase, case):
        return subprocess.run([sys.executable, '-B', '-c', driver, str(script),
                               str(output / case), phase, case], capture_output=True, text=True, timeout=15)
    smoke = run('smoke', 'smoke-one')
    assert smoke.returncode == 0, smoke.stderr
    failed = run('main', 'contrast')
    assert failed.returncode == 1 and 'Recoverable input staging error' in failed.stderr
    assert 'PermissionError' not in failed.stderr
    recovered = run('main', 'contrast')
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads((output / 'contrast/summary.json').read_text())['attempt'] == 2
    assert (output / 'contrast/attempt-1/failure.json').exists()
