"""Read-only cluster discovery, including externally managed MPS clients."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

import gpu_mcp_mps_inspection as probe
from test_gpu_mcp_server_contract import repo_fixture, _write_config, _import_server_with_config

pytestmark = pytest.mark.contract


def test_mps_probe_uses_only_read_commands_and_each_servers_private_endpoint(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    pipes = {40: '/tmp/private mps;ignored', 41: '/tmp/other-mps'}
    for pid, pipe in pipes.items():
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / 'stat').write_text(f'{pid} (nvidia-cuda-mps-server) S ' + '0 ' * 18 + '1234 0')
        (directory / 'environ').write_bytes(f'CUDA_MPS_PIPE_DIRECTORY={pipe}\0SECRET=not-for-the-report\0'.encode())
    calls = tmp_path / 'calls.jsonl'
    binary = tmp_path / 'nvidia-cuda-mps-control'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys
command = sys.stdin.read().strip()
pipe = os.environ['CUDA_MPS_PIPE_DIRECTORY']
with open({str(calls)!r}, 'a') as stream:
    stream.write(json.dumps([pipe, command]) + '\\n')
pid = {{value: key for key, value in {pipes!r}.items()}}[pipe]
if command == 'get_server_list': print(pid)
elif command == f'get_client_list {{pid}}': print(pid + 100)
elif command == f'ps -p {{pid}}':
    print('PID ID SERVER DEVICE NAMESPACE COMMAND')
    print(f'{{pid + 100}} 1 {{pid}} GPU-abcd 4026531836 python outside-repo.py')
else: sys.exit(2)
''')
    binary.chmod(0o755)
    monkeypatch.setattr(probe.shutil, 'which', lambda name: str(binary))
    monkeypatch.setattr(probe, 'Path', lambda path: proc / str(path).removeprefix('/proc/'))
    result = probe.inspect_mps_servers([40, 41])
    assert result == {'servers': [
        {'pid': pid, 'clients': [{'pid': pid + 100, 'gpu_uuid_prefixes': ['GPU-abcd']}]}
        for pid in pipes]}
    assert 'not-for-the-report' not in json.dumps(result)
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        [pipe, command] for pid, pipe in pipes.items()
        for command in ('get_server_list', f'get_client_list {pid}', f'ps -p {pid}')]


@pytest.mark.parametrize('command', ['quit', 'start_server -uid 1000', 'terminate_client 40 140', 'get_client_list 40;quit'])
def test_mps_probe_refuses_mutating_or_injected_commands(command, monkeypatch):
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **k: pytest.fail('control command executed'))
    with pytest.raises(ValueError, match='unsupported'):
        probe._query('/usr/bin/nvidia-cuda-mps-control', '/tmp/mps', command, float('inf'))


@pytest.mark.parametrize('failure,reason', [
    (PermissionError(), 'permission denied'),
    (FileNotFoundError(), 'exited'),
    (subprocess.TimeoutExpired('mps-control', 2), 'timed out'),
])
def test_mps_probe_reports_unavailable_clients(failure, reason, monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda name: '/usr/bin/nvidia-cuda-mps-control')
    monkeypatch.setattr(probe, '_start_time', lambda pid: '1234')
    def fail(pid):
        raise failure
    monkeypatch.setattr(probe, '_server_environment', fail)
    result = probe.inspect_mps_servers([40])['servers'][0]
    assert reason in result['error'] and result['clients'] == []


@pytest.mark.parametrize('servers,clients,expected', [
    ('99', '140', 'does not list'),
    ('40', '140;quit', 'invalid PID'),
    ('40', '', None),
])
def test_mps_probe_verifies_membership_and_distinguishes_empty_from_failure(servers, clients, expected, monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda name: '/usr/bin/nvidia-cuda-mps-control')
    monkeypatch.setattr(probe, '_start_time', lambda pid: '1234')
    monkeypatch.setattr(probe, '_server_environment', lambda pid: '/tmp/mps')
    monkeypatch.setattr(probe, '_query', lambda binary, pipe, command, deadline:
                        servers if command == 'get_server_list' else clients)
    result = probe.inspect_mps_servers([40])['servers'][0]
    if expected:
        assert expected in result['error']
    else:
        assert result == {'pid': 40, 'clients': []}


def test_mps_probe_keeps_membership_when_older_driver_has_no_ps_and_rejects_reused_server(monkeypatch):
    monkeypatch.setattr(probe.shutil, 'which', lambda name: '/usr/bin/nvidia-cuda-mps-control')
    monkeypatch.setattr(probe, '_server_environment', lambda pid: '/tmp/mps')
    monkeypatch.setattr(probe, '_query', lambda binary, pipe, command, deadline:
                        {'get_server_list': '40', 'get_client_list 40': '140', 'ps -p 40': 'Unknown command'}[command])
    monkeypatch.setattr(probe, '_start_time', lambda pid: '1234')
    result = probe.inspect_mps_servers([40])['servers'][0]
    assert result['clients'] == [{'pid': 140, 'gpu_uuid_prefixes': []}]
    assert result['device_mapping_unavailable']
    starts = iter(['1234', '1235'])
    monkeypatch.setattr(probe, '_start_time', lambda pid: next(starts))
    result = probe.inspect_mps_servers([40])['servers'][0]
    assert 'changed' in result['error'] and result['clients'] == []


@pytest.fixture()
def inspector(repo_fixture, monkeypatch):
    server = _import_server_with_config(monkeypatch, _write_config(repo_fixture))
    data = {'gpu': '0, GPU-aaaa-full\n1, GPU-bbbb-full',
            'processes': '10, GPU-aaaa-full, 2048, python\n40, GPU-bbbb-full, 900, nvidia-cuda-mps-server',
            'mps': {'servers': [{'pid': 40, 'clients': [{'pid': 140, 'gpu_uuid_prefixes': ['GPU-bbbb']}]}]},
            'owners': {10: f'{server.GPU_MCP_USER} python /another-repo/train.py',
                       40: 'colleague nvidia-cuda-mps-server',
                       140: 'colleague python /human-managed/run.py --seed 1'}, 'calls': []}
    def host_run(host, command, **kwargs):
        data['calls'].append((host, command, kwargs))
        if 'query-compute-apps' in command:
            return data['processes']
        if 'query-gpu=index,uuid' in command:
            return data['gpu']
        if 'inspect_mps_servers' in command:
            return json.dumps(data['mps']) if data['mps'] is not None else None
        if command.startswith('ps -p'):
            return data['owners'].get(int(command.split()[2]))
        pytest.fail(f'unexpected inspection command: {command}')
    monkeypatch.setattr(server, '_host_run', host_run)
    yield server, data
    server.HEARTBEAT_MANAGER.stop()


def test_external_mps_clients_are_visible_without_managed_job_records(inspector):
    server, data = inspector
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'GPU 0 | PID 10' in result and 'role=cuda' in result
    assert 'GPU 1 | PID 40 | user=colleague | mem=900 MiB | role=mps-server' in result
    assert 'GPU 1 | PID 140 | user=colleague | mem=unavailable | role=mps-client (server PID 40)' in result
    assert '/human-managed/run.py --seed 1' in result
    assert '900 MiB' not in next(line for line in result.splitlines() if 'PID 140 |' in line)
    assert not list((server.REPO_ROOT / '.gpu_mcp_state').glob('jobs/job-*'))


def test_process_user_filters_apply_to_external_mps_clients(inspector):
    server, _ = inspector
    own = server.check_gpu_processes(hosts=['gpu-a'])
    assert 'PID 10' in own and 'PID 140' not in own
    colleague = server.check_gpu_processes(hosts=['gpu-a'], user_filter='colleague')
    assert 'PID 140' in colleague and 'PID 10' not in colleague


def test_unapproved_hosts_and_invalid_arguments_never_reach_transport(inspector):
    server, data = inspector
    assert 'rejected' in server.check_gpu_processes(hosts=['unapproved'])
    assert 'ERROR' in server.check_gpu_processes(hosts='gpu-a')
    assert 'ERROR' in server.check_gpu_processes(user_filter=12)
    assert data['calls'] == []


def test_invalid_gpu_pid_is_not_interpolated_into_a_command(inspector):
    server, data = inspector
    data['processes'] = '12;touch /tmp/injected, GPU-aaaa-full, 1, python'
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'incomplete' in result
    assert all('ps -p' not in command for _, command, _ in data['calls'])


@pytest.mark.parametrize('report', [None, {'servers': []},
    {'servers': [{'pid': 40, 'clients': None}]},
    {'servers': [{'pid': 40, 'error': 'permission denied'}]}])
def test_unavailable_mps_details_preserve_ordinary_process_report(inspector, report):
    server, data = inspector
    data['mps'] = report
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'PID 10' in result and 'role=mps-server' in result
    assert 'client information unavailable' in result
    assert 'no attached clients' not in result
    assert 'MPS membership unknown' in result


def test_directly_reported_mps_client_is_not_duplicated_and_retains_measured_memory(inspector):
    server, data = inspector
    data['processes'] += '\n140, GPU-bbbb-full, 123, python'
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert result.count('PID 140 |') == 1
    assert 'mem=123 MiB | role=mps-client' in result


def test_multi_gpu_mps_server_does_not_assign_every_client_to_every_gpu(inspector):
    server, data = inspector
    data['processes'] += '\n40, GPU-aaaa-full, 400, nvidia-cuda-mps-server'
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'GPU 1 | PID 140' in result and 'GPU 0 | PID 140' not in result
    data['mps']['servers'][0]['clients'][0]['gpu_uuid_prefixes'] = []
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'GPU ? | PID 140' in result and 'per-client GPU unavailable' in result


def test_membership_alone_does_not_invent_a_client_gpu_assignment(inspector):
    server, data = inspector
    data['mps']['servers'][0]['clients'][0]['gpu_uuid_prefixes'] = []
    result = server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert 'GPU ? | PID 140' in result and 'server PID 40 observed on GPU(s) 1' in result


def test_configured_ssh_identity_and_canonical_host_are_used(inspector):
    server, data = inspector
    server.NODES = ['configured-user@gpu-a.example.org']
    server._GPU_MCP_USER_ENV = ''
    server.check_gpu_processes(hosts=['gpu-a'], user_filter='')
    assert all(host == 'gpu-a.example.org' and options['user'] == 'configured-user'
               for host, _, options in data['calls'])
