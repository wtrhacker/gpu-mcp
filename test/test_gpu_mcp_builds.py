from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import signal
import time
import tempfile

import pytest

import gpu_mcp_build_worker as worker
from gpu_mcp_config import ConfigError, load_policy
from gpu_mcp_policy_approval import diff_policy_summary, policy_summary
import gpu_mcp_guard


@pytest.fixture
def build_spec(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    spec = dict(job_id='job-20261001T000000Z-buildtest', active_attempt_id='attempt-fixture',
                nonce='test-owned-attempt', repo_root=str(repo), script_path=str(repo / 'build.py'),
                script_roots=[str(repo)], write_roots=[str(repo)], output_roots=[str(repo)],
                output_file=str(repo / 'build.log'), artifact_path=str(repo / 'answer.so'),
                build_mode='cpu_only', args=[], worker_state_root=str(tmp_path / 'control'))
    return spec


def run_build(spec, body):
    if not shutil.which('bwrap') or not shutil.which('cc'):
        pytest.skip('local build acceptance needs bubblewrap and a C compiler')
    Path(spec['script_path']).write_text(body)
    worker.rpc('launch', spec, Path(worker.__file__).read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = worker.inspect(spec)
        if result['state'] == 'gone' and result.get('outcome'):
            return result
        time.sleep(0.03)
    worker.rpc('stop', spec)
    pytest.fail('build did not complete: ' + Path(spec['output_file']).read_text())


def test_real_cpu_build_runs_without_gpu_devices(build_spec):
    result = run_build(build_spec, '''
import os, subprocess
from pathlib import Path
assert os.environ['CUDA_VISIBLE_DEVICES'] == ''
assert not list(Path('/dev').glob('nvidia*'))
subprocess.run(['cc', '-shared', '-fPIC', '-x', 'c', '-', '-o', os.environ['GPU_MCP_ARTIFACT_PATH']],
    input='double square(double x) { return x*x; }', text=True, check=True)
''')
    assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    library = ctypes.CDLL(build_spec['artifact_path'])
    library.square.argtypes = [ctypes.c_double]
    library.square.restype = ctypes.c_double
    assert library.square(3.5) == 12.25


def test_build_honors_whole_repo_writes_and_protects_outside(build_spec, tmp_path):
    # /tmp is intentionally private scratch in the worker. Use a real host
    # file outside that scratch view to test the host-write boundary.
    fixtures = Path(__file__).resolve().parents[1] / 'test_mcp_repos'
    fixtures.mkdir(exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=fixtures, prefix='build-outside-', delete=False)
    outside = Path(handle.name)
    handle.close()
    outside.write_text('original')
    repo = Path(build_spec['repo_root'])
    (repo / 'source.txt').write_text('old')
    (repo / 'escape').symlink_to(outside)
    result = run_build(build_spec, f'''
import os, subprocess
from pathlib import Path
Path('source.txt').write_text('new')
Path('temporary.txt').write_text('delete me')
Path('temporary.txt').unlink()
for target in ['escape', {str(outside)!r}]:
    proc = subprocess.run(['sh', '-c', 'echo bad > "$1"', 'probe', target])
    assert proc.returncode != 0
Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('done')
''')
    assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    assert outside.read_text() == 'original'
    assert (repo / 'source.txt').read_text() == 'new'


def test_build_can_produce_a_package_directory(build_spec):
    build_spec['artifact_path'] = str(Path(build_spec['repo_root']) / 'package')
    result = run_build(build_spec, '''
import os
from pathlib import Path
package = Path(os.environ['GPU_MCP_ARTIFACT_PATH'])
package.mkdir()
(package/'__init__.py').write_text('answer=42')
''')
    assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    assert result['outcome']['artifact']['kind'] == 'directory'


def test_build_enforces_a_narrow_write_policy(build_spec):
    repo = Path(build_spec['repo_root'])
    results = repo / 'results'
    results.mkdir()
    build_spec['write_roots'] = [str(results)]
    build_spec['artifact_path'] = str(results / 'package')
    result = run_build(build_spec, '''
import os
from pathlib import Path
try:
    Path('source.txt').write_text('not permitted')
except OSError:
    pass
else:
    raise RuntimeError('source directory unexpectedly writable')
Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('result')
''')
    assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    assert not (repo / 'source.txt').exists()


def test_captured_roots_do_not_follow_later_aliases(tmp_path):
    permitted = tmp_path / 'permitted'
    forbidden = tmp_path / 'forbidden'
    forbidden.mkdir()
    permitted.symlink_to(forbidden, target_is_directory=True)
    assert not worker.inside(permitted / 'output', [str(permitted)])


def test_zero_exit_missing_artifact_is_failure(build_spec):
    result = run_build(build_spec, "print('no artifact')\n")
    assert result['outcome']['terminal_status'] == 'failure'
    assert result['outcome']['artifact'] is None


def test_cpu_build_cannot_gain_gpu_via_environment(build_spec):
    result = run_build(build_spec, '''
import os
os.environ['CUDA_VISIBLE_DEVICES']='0'
open('/dev/nvidia0', 'rb')
''')
    assert result['outcome']['terminal_status'] == 'failure'


def test_host_unix_socket_is_denied_but_local_socketpair_works(build_spec):
    sock = socket.socket(socket.AF_UNIX)
    path = str(Path(build_spec['repo_root']) / 'host-service.sock')
    sock.bind(path)
    sock.listen()
    try:
        result = run_build(build_spec, f'''
import socket, os
from pathlib import Path
a,b=socket.socketpair()
a.send(b'local')
assert b.recv(5)==b'local'
client=socket.socket(socket.AF_UNIX)
try:
    client.connect({path!r})
except PermissionError:
    pass
else:
    raise RuntimeError('host socket was reachable')
Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('done')
''')
        assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    finally:
        sock.close()


def test_build_capability_is_visible_and_typed(tmp_path):
    path = tmp_path / 'gpu-mcp.toml'
    text = f"schema_version=1\nrepo_root={str(tmp_path)!r}\nnodes=['localhost']\nscript_roots=['.']\nwrite_roots=['.']\noutput_roots=['.']\n"
    path.write_text(text)
    before = load_policy(path)
    assert not before.build_enabled
    path.write_text(text + 'build_enabled=true\n')
    after = load_policy(path)
    assert policy_summary(after)['build_enabled']
    assert 'changed build_enabled from False to True' in diff_policy_summary(before, after)
    path.write_text(text + "build_enabled='yes'\n")
    with pytest.raises(ConfigError, match='boolean'):
        load_policy(path)


def test_normal_runner_allows_only_real_null_device_outside_roots(tmp_path):
    hook = gpu_mcp_guard.make_safe_run_audit_hook([tmp_path / 'approved'])
    hook('open', ('/dev/null', 'r+b', os.O_RDWR))
    for target in ('/dev/zero', str(tmp_path / 'null'), str(tmp_path / 'outside.txt')):
        with pytest.raises(PermissionError):
            hook('open', (target, 'wb', os.O_WRONLY | os.O_CREAT))
    alias = tmp_path / 'null-link'
    alias.symlink_to('/dev/null')
    with pytest.raises(PermissionError):
        hook('open', (alias, 'wb', os.O_WRONLY))


@pytest.fixture
def managed_server(tmp_path, monkeypatch):
    from test_gpu_mcp_server_contract import _import_server_with_config
    repo = tmp_path / 'repo'
    (repo / 'logs').mkdir(parents=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=['localhost']\n"
                      "script_roots=['.']\nwrite_roots=['.']\noutput_roots=['logs']\n"
                      'build_enabled=true\nsync_timeout_sec=10\n')
    monkeypatch.setenv('GPU_MCP_TEST_RESERVATION_ROOT', str(tmp_path / 'control' / 'reservations'))
    monkeypatch.setenv('GPU_MCP_TEST_BUILD_WORKER_ROOT', str(tmp_path / 'workers'))
    server = _import_server_with_config(monkeypatch, config)
    yield server, repo
    server.HEARTBEAT_MANAGER.stop()


def wait_managed(server, job_id):
    for _ in range(100):
        result = json.loads(server.manage_gpu_job(action='status', job_id=job_id,
                                                 early_poll_reason='short test completion check'))
        if result.get('job_lifecycle') in {'succeeded', 'failed'}:
            return result
        time.sleep(0.03)
    pytest.fail(str(result))


def test_cpu_managed_build_ignores_gpu_health_and_forged_mirror(managed_server, monkeypatch):
    server, repo = managed_server
    monkeypatch.setattr(server.HEARTBEAT_MANAGER, 'is_healthy', lambda: False)
    monkeypatch.setattr(server, '_process_gpu_usage', lambda *a, **k: pytest.fail('CPU build queried GPU'))
    (repo / 'build.py').write_text("import os\nfrom pathlib import Path\nPath(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('done')\n")
    launched = json.loads(server.build_python_artifact(host='localhost', script_path='build.py',
                         artifact_path='artifact.txt', output_file='logs/build.log', cadence_hint_sec=1))
    assert launched['status'] == 'launched', launched
    assert launched['gpu_index'] is None and launched['reservation_key'] is None
    mirror = server.reservations.job_record_path(repo, launched['job_id'])
    mirror.write_text('{"job_lifecycle":"forged","server_instance_id":"wrong"}')
    result = wait_managed(server, launched['job_id'])
    assert result['job_lifecycle'] == 'succeeded', result
    assert result['artifact']['bytes'] == 4
    retry = json.loads(server.manage_gpu_job(action='retry', job_id=launched['job_id']))
    assert retry['status'] == 'retried', retry
    assert retry['attempt_id'] != launched['attempt_id']
    assert wait_managed(server, launched['job_id'])['job_lifecycle'] == 'succeeded'
    finished = json.loads(server.manage_gpu_job(action='finish', job_id=launched['job_id']))
    assert finished['status'] == 'finished'


def test_build_mode_and_capability_refusals(managed_server):
    server, repo = managed_server
    (repo / 'build.py').write_text('pass\n')
    for mode, index in [('cpu_only', 0), ('gpu_assisted', None), ('unknown', None)]:
        result = json.loads(server.build_python_artifact(host='localhost', script_path='build.py',
                            artifact_path='artifact', build_mode=mode, gpu_index=index))
        assert result['status'] == 'refused'
    from dataclasses import replace
    server.CONFIG_POLICY = replace(server.CONFIG_POLICY, build_enabled=False)
    result = json.loads(server.build_python_artifact(host='localhost', script_path='build.py', artifact_path='artifact'))
    assert result['status'] == 'refused' and 'build_enabled' in result['reason']


def test_gpu_identity_uses_device_minor_and_busy_probe_releases_claim(managed_server, monkeypatch):
    server, repo = managed_server
    manager = server._build_manager()
    xml = '<nvidia_smi_log><gpu><uuid>GPU-test</uuid><minor_number>3</minor_number><product_name>GPU</product_name><fb_memory_usage><free>22000 MiB</free></fb_memory_usage><processes>{processes}</processes></gpu></nvidia_smi_log>'
    monkeypatch.setattr(server, '_host_run', lambda host, command, **k:
                        '' if '--query-compute-apps' in command else xml.format(processes=''))
    uuid, devices = manager.probe_gpu('localhost', 1)
    assert uuid == 'GPU-test' and '/dev/nvidia3' in devices and '/dev/nvidia1' not in devices
    (repo / 'build.py').write_text('pass\n')
    monkeypatch.setattr(server, '_host_run', lambda host, command, **k:
                        'GPU-test, 42' if '--query-compute-apps' in command else xml.format(processes=''))
    result = json.loads(server.build_python_artifact(host='localhost', script_path='build.py', artifact_path='artifact',
                                                   output_file='logs/build.log', build_mode='gpu_assisted', gpu_index=1))
    assert result['status'] == 'refused' and 'process' in result['reason']
    assert not (server.reservations.reservation_registry_root() / 'localhost.gpu1').exists()


def test_detached_helper_dies_with_completed_build(build_spec):
    result = run_build(build_spec, '''
import os, subprocess, sys
from pathlib import Path
subprocess.Popen([sys.executable, '-c', 'import time; from pathlib import Path; time.sleep(1); Path("leaked").write_text("bad")'],
                 start_new_session=True)
Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('done')
''')
    assert result['outcome']['terminal_status'] == 'success', Path(build_spec['output_file']).read_text()
    state = result['process']
    assert not worker.same_process(state['namespace'])
    time.sleep(1.1)
    assert not (Path(build_spec['repo_root']) / 'leaked').exists()


@pytest.mark.parametrize('kill_supervisor', [False, True])
def test_stop_or_supervisor_loss_ends_namespace(managed_server, kill_supervisor):
    server, repo = managed_server
    (repo / 'build.py').write_text('''
import subprocess,sys,time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'], start_new_session=True)
Path('ready').write_text('ready')
time.sleep(300)
''')
    result = json.loads(server.build_python_artifact(host='localhost', script_path='build.py',
                         artifact_path='artifact', output_file='logs/build.log', cadence_hint_sec=1))
    assert result['status'] == 'launched'
    manager = server._build_manager()
    for _ in range(100):
        if (repo / 'ready').exists():
            break
        time.sleep(.02)
    assert (repo / 'ready').exists(), (repo / 'logs/build.log').read_text()
    record = manager.read(result['job_id'])
    if kill_supervisor:
        os.kill(record['remote_pid'], signal.SIGKILL)
    else:
        stopped = json.loads(server.manage_gpu_job(action='stop', job_id=result['job_id']))
        assert stopped['status'] == 'stop_requested'
    for _ in range(100):
        observed = manager.inspect(record)
        if observed['state'] == 'gone':
            break
        time.sleep(.02)
    assert observed['state'] == 'gone', observed
    finished = json.loads(server.manage_gpu_job(action='finish', job_id=result['job_id']))
    assert finished['status'] == 'finished', finished
