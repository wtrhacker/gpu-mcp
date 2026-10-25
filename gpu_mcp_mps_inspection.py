"""Read-only MPS v2 client discovery, sent as source to the execution host.

Uses only NVIDIA's inspection commands and the observed servers' /proc entries.
It knows nothing about GPU MCP job state or an external manager's queues.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def _pids(raw):
    words = raw.split()
    if any(not word.isascii() or not word.isdecimal() or int(word) <= 0 for word in words):
        raise ValueError('MPS returned an invalid PID list')
    return sorted({int(word) for word in words})


def _start_time(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in {'Z', 'X', 'x'}:
        raise ValueError('MPS server exited during inspection')
    return fields[19]


def _server_environment(pid):
    # Read only these two values; never return the process's environment.
    with Path(f'/proc/{pid}/environ').open('rb') as stream:
        raw = stream.read(1024 * 1024)
    selected = {}
    for item in raw.split(b'\0'):
        key, sep, value = item.partition(b'=')
        if sep and key in {b'CUDA_MPS_PIPE_DIRECTORY', b'CUDA_MPS_PROTOCOL_VERSION'}:
            selected[key.decode()] = os.fsdecode(value)
    if selected.get('CUDA_MPS_PROTOCOL_VERSION', '2') != '2':
        raise ValueError('MPS client inspection supports protocol 2')
    return selected.get('CUDA_MPS_PIPE_DIRECTORY') or '/tmp/nvidia-mps'


def _query(binary, pipe_directory, command, deadline):
    # This allowlist also prevents a future caller from turning discovery into
    # a control operation. No daemon startup, shutdown, or client termination.
    words = command.split()
    if not (words == ['get_server_list'] or
            (len(words) == 2 and words[0] == 'get_client_list' and words[1].isdecimal()) or
            (len(words) == 3 and words[:2] == ['ps', '-p'] and words[2].isdecimal())):
        raise ValueError('unsupported MPS inspection command')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('MPS inspection time budget exhausted')
    env = dict(os.environ, CUDA_MPS_PIPE_DIRECTORY=pipe_directory, CUDA_MPS_PROTOCOL_VERSION='2')
    result = subprocess.run([binary], input=command + '\n', env=env,
                            capture_output=True, text=True, timeout=min(2, remaining))
    if result.returncode or result.stdout.lstrip().lower().startswith(('error', 'failed', 'invalid')):
        raise ValueError('MPS inspection command failed')
    return result.stdout


def _client_devices(raw, server_pid):
    """Parse NVIDIA's `ps` table, keeping its partial device UUIDs explicit."""
    lines = [line.split() for line in raw.splitlines() if line.strip()]
    if not lines or not {'PID', 'SERVER', 'DEVICE'}.issubset(lines[0]):
        raise ValueError('MPS client device mapping unavailable')
    columns = {key: lines[0].index(key) for key in ('PID', 'SERVER', 'DEVICE')}
    devices = {}
    for words in lines[1:]:
        if len(words) <= max(columns.values()):
            continue
        pid, server, device = (words[columns[key]] for key in ('PID', 'SERVER', 'DEVICE'))
        if server != str(server_pid) or not pid.isascii() or not pid.isdecimal() or int(pid) <= 0:
            continue
        if device.startswith(('GPU-', 'MIG-')):
            devices.setdefault(int(pid), set()).add(device)
    return {pid: sorted(values) for pid, values in devices.items()}


def inspect_mps_servers(server_pids):
    """Return attached client PIDs or an explicit unavailable reason per server."""
    binary = shutil.which('nvidia-cuda-mps-control')
    deadline = time.monotonic() + 10
    endpoints = {}
    reports = []
    for pid in server_pids:
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid MPS server PID')
        report = {'pid': pid, 'clients': []}
        reports.append(report)
        try:
            if binary is None:
                raise ValueError('nvidia-cuda-mps-control is unavailable')
            before = _start_time(pid)
            pipe = _server_environment(pid)
            if pipe not in endpoints:
                endpoints[pipe] = _pids(_query(binary, pipe, 'get_server_list', deadline))
            if pid not in endpoints[pipe]:
                raise ValueError('MPS endpoint does not list the observed server')
            clients = _pids(_query(binary, pipe, f'get_client_list {pid}', deadline))
            devices = {}
            if clients:
                try:
                    devices = _client_devices(_query(binary, pipe, f'ps -p {pid}', deadline), pid)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    # Membership is still useful on older drivers without ps.
                    report['device_mapping_unavailable'] = True
            if _start_time(pid) != before:
                raise ValueError('MPS server changed during inspection')
            report['clients'] = [{'pid': client, 'gpu_uuid_prefixes': devices.get(client, [])}
                                 for client in clients]
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
            if isinstance(exc, PermissionError):
                reason = 'permission denied reading MPS server information'
            elif isinstance(exc, FileNotFoundError):
                reason = 'MPS server exited or its inspection utility disappeared'
            elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
                reason = 'MPS client inspection timed out'
            else:
                reason = str(exc)
            report['error'] = reason
    return {'servers': reports}


if __name__ == '__main__':
    print(json.dumps(inspect_mps_servers(json.loads(sys.argv[1]))))
