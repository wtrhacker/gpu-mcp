"""A process can exit between ps and reading its fingerprint from /proc."""
import pytest

from test_gpu_mcp_codex_uncertainty import launched_server

pytestmark = pytest.mark.contract


@pytest.mark.parametrize('second_probe', ['empty', 'missing-exit', 'zombie', 'other-owner', 'alive', 'transport-failure'])
def test_fingerprint_read_failure_rechecks_liveness_without_guessing(launched_server, second_probe):
    f = launched_server
    probes = []
    row = f'42 {f.server.GPU_MCP_USER} S Sat Oct 10 00:00:00 2026'

    def run(host, command, **kwargs):
        if command.startswith('ps -p 42 '):
            probes.append(command)
            if len(probes) == 1 or second_probe == 'alive':
                return row
            if second_probe == 'empty':
                return ''
            if second_probe == 'zombie':
                return row.replace(' S ', ' Z ')
            if second_probe == 'other-owner':
                return row.replace(f.server.GPU_MCP_USER, 'another-user')
            f.server._record_host_run_error(host, command,
                'exit 1: ' if second_probe == 'missing-exit' else 'timeout: process query unavailable')
            return None
        f.server._record_host_run_error(host, command, 'FileNotFoundError: /proc/42/environ')
        return None

    f.monkeypatch.setattr(f.server, '_host_run', run)
    observation = f.server._inspect_reservation_process({
        'host': 'localhost', 'remote_pid': 42, 'owner_user': f.server.GPU_MCP_USER,
        'process_fingerprint': 'gpu-mcp-process:original', 'remote_start_time': None,
        'server_instance_id': f.server.SERVER_INSTANCE_ID, 'reservation_key': 'localhost.gpu0'})
    assert observation['status'] == ('gone' if second_probe in {'empty', 'missing-exit', 'zombie', 'other-owner'} else 'unknown')
    assert len(probes) == 2
