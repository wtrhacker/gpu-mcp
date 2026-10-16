"""Recover exhausted GPU MCP control processes without signalling GPU jobs."""
from __future__ import annotations

from contextlib import closing
import ctypes
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import time
import tomllib
import urllib.error

import gpu_mcp_codex_launcher as launcher
import gpu_mcp_reservations as reservations
from gpu_mcp_codex_config import shared_entrypoint
from gpu_mcp_codex_state import private_directory, runtime_root

# A shared backend has a small, bounded request pool. Hundreds of persistent
# transports together with a failing health probe indicate resource exhaustion.
LEAK_MIN_TRANSPORTS = 256
IDENTITY_FIELDS = ('pid', 'process_start', 'server_instance_id', 'config_path', 'hostname')
TERMINAL = {'succeeded', 'failed', 'finished', 'process_gone_unknown_outcome'}


def _pidfd_syscall(number, *args):
    # Some Conda Python builds omit pidfd wrappers despite kernel support.
    # These syscall numbers are shared by Linux x86-64 and AArch64.
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'aarch64'}:
        raise ValueError('safe automatic GPU MCP recovery requires Linux pidfd support')
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.restype = ctypes.c_long
    result = syscall(ctypes.c_long(number), *args)
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def pidfd_open(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    return _pidfd_syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def pidfd_signal(fd, value):
    if hasattr(signal, 'pidfd_send_signal'):
        return signal.pidfd_send_signal(fd, value)
    return _pidfd_syscall(424, ctypes.c_int(fd), ctypes.c_int(value), ctypes.c_void_p(), ctypes.c_uint(0))


def resources(pid, *, proc_root=Path('/proc')):
    proc = proc_root / str(pid)
    status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
    sockets = 0
    descriptors = list((proc / 'fd').iterdir())
    for fd in descriptors:
        try:
            sockets += str(fd.readlink()).startswith('socket:[')
        except FileNotFoundError:
            continue
    return {'threads': int(status['Threads']), 'sockets': sockets, 'descriptors': len(descriptors)}


def running_servers(*, proc_root=Path('/proc')):
    """Discover owned MCP controllers, including ones absent from saved history."""
    found = []
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            argv = (proc / 'cmdline').read_bytes().decode().strip('\0').split('\0')
            index = 1
            while index < len(argv) and argv[index] in {'-B', '-u', '-E', '-s'}:
                index += 1
            if index >= len(argv):
                continue
            script = Path(argv[index])
            if (not script.is_absolute() or script.name not in {'gpu_mcp_server.py', 'gpu_mcp_backend.py'}
                    or '--safe-run' in argv or '--supervise' in argv or argv.count('--config') != 1):
                continue
            config = Path(argv[argv.index('--config') + 1])
            if not config.is_absolute():
                continue
            executable = (proc / 'exe').resolve(strict=True)
            if not executable.name.startswith('python'):
                continue
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] in {'Z', 'X'}:
                continue
            found.append({'pid': int(proc.name), 'process_start': fields[19],
                          'repo_root': str(config.resolve().parent), 'config_path': str(config.resolve()),
                          'runtime': str(script.resolve().parent), 'python': str(executable),
                          'shared': '--codex-shared' in argv, 'resources': resources(int(proc.name), proc_root=proc_root)})
        except (OSError, ValueError, IndexError, KeyError):
            continue
    return found


def verify_process(plan, info):
    """Check OS identity as well as protected metadata before any signal."""
    root = plan['root']
    for name in ('config', 'registration'):
        if name + '_hash' in plan and hashlib.sha256(plan[name].read_bytes()).hexdigest() != plan[name + '_hash']:
            raise ValueError('GPU MCP policy or registration changed during repair; rerun')
    reservations._assert_no_symlink_ancestors(root / 'backend.json')
    if root.stat().st_uid != os.getuid():
        raise ValueError('GPU MCP state belongs to another user')
    current = launcher.read_info(root)
    if not isinstance(current, dict) or any(current.get(k) != info.get(k) for k in IDENTITY_FIELDS):
        raise ValueError('GPU MCP identity changed during repair; rerun')
    if not launcher.alive(info) or info.get('hostname') != socket.gethostname():
        raise ValueError('GPU MCP process identity is no longer current')
    proc = Path('/proc') / str(info['pid'])
    if proc.stat().st_uid != os.getuid() or proc.joinpath('exe').resolve() != Path(plan['python']).resolve():
        raise ValueError('GPU MCP process owner or interpreter differs; no signal sent')
    argv = (proc / 'cmdline').read_bytes().decode().split('\0')
    expected = {'--config': str(plan['config']), '--runtime-root': str(root)}
    if (len(argv) < 2 or argv[1] not in {str(plan['runtime'] / name) for name in ('gpu_mcp_server.py', 'gpu_mcp_backend.py')}
            or '--codex-shared' not in argv):
        raise ValueError('process is not this installation\'s shared GPU MCP server')
    for flag, value in expected.items():
        if argv.count(flag) != 1 or argv.index(flag) + 1 >= len(argv) or argv[argv.index(flag) + 1] != value:
            raise ValueError('GPU MCP process arguments differ; no signal sent')


def saved_jobs(plan):
    path = plan['root'] / 'jobs.sqlite3'
    reservations._assert_no_symlink_ancestors(path)
    if not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError('protected job database is unavailable; repair stopped')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT record FROM jobs')]
    for record in records:
        if (not isinstance(record, dict) or not reservations.is_job_id(record.get('job_id'))
                or not reservations.is_attempt_id(record.get('active_attempt_id'))
                or record.get('repo_root', str(plan['config'].parent)) != str(plan['config'].parent)):
            raise ValueError('protected job identity is invalid; repair stopped')
    return records


def healthy(info):
    try:
        result = launcher.request(info, '/health', timeout=2)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError('GPU MCP credentials were refused; no forced repair') from exc
        return False
    except OSError:
        return False
    if not isinstance(result, dict) or result.get('server_instance_id') != info.get('server_instance_id'):
        raise ValueError('GPU MCP endpoint identity differs; repair stopped')
    return True


def exhausted(observed):
    return min(observed['threads'], observed['sockets']) >= LEAK_MIN_TRANSPORTS


def stop_exhausted(plan, info, *, grace=5):
    """A pidfd pins one process even if its numeric PID is later reused."""
    verify_process(plan, info)
    with closing(os.fdopen(pidfd_open(info['pid']), 'rb', buffering=0)) as pinned:
        verify_process(plan, info)
        if not exhausted(resources(info['pid'])) or healthy(info):
            raise ValueError('GPU MCP recovered or its resource state changed; no forced restart')
        pidfd_signal(pinned.fileno(), signal.SIGTERM)
        if not select.select([pinned], [], [], grace)[0]:
            verify_process(plan, info)
            pidfd_signal(pinned.fileno(), signal.SIGKILL)
            if not select.select([pinned], [], [], 10)[0]:
                raise ValueError('GPU MCP has not exited; no replacement was started')


def start_backend(plan):
    program = ('import json,sys;from gpu_mcp_codex_launcher import ensure_backend;'
               'print(json.dumps(ensure_backend(sys.argv[1],base=sys.argv[2])))')
    result = subprocess.run([plan['python'], '-B', '-c', program, str(plan['config']), str(plan['root'].parent)],
                            cwd=plan['runtime'], env=dict(os.environ, **plan['server'].get('env', {})),
                            capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise ValueError('GPU MCP restart failed; saved jobs/reservations were retained. ' + result.stderr[-1200:])
    info = json.loads(result.stdout)
    if not healthy(info):
        raise ValueError('replacement GPU MCP did not pass its health check')
    return info


def inspect_backend(plan):
    info = launcher.read_info(plan['root'])
    if not launcher.alive(info):
        return None
    if info.get('config_path') != str(plan['config']):
        raise ValueError('GPU MCP policy identity differs; repair stopped')
    verify_process(plan, info)
    observed = resources(info['pid'])
    if exhausted(observed) and not healthy(info):
        saved_jobs(plan)
        return {'info': info, 'resources': observed, 'action': 'recover_exhausted'}
    if info.get('code_hash') != launcher.code_hash():
        snapshot = launcher.request(info, '/human/snapshot', human=True, timeout=3)
        if snapshot.get('server_instance_id') != info.get('server_instance_id'):
            raise ValueError('GPU MCP snapshot identity differs; repair stopped')
        active = [j for j in snapshot['jobs'] if j.get('server_instance_id') == info['server_instance_id']
                  and (j.get('job_lifecycle') not in TERMINAL
                       or j.get('active_reservation', bool(j.get('reservation_key'))))]
        return {'info': info, 'resources': observed, 'action': 'defer_busy' if active else 'refresh_idle'}
    return None


def repair(runtime, repos, *, home=None, preview=False, processes=()):
    """Run before optional registration/dependency migrations, including on retry."""
    runtime = Path(runtime).resolve()
    home = Path(home or Path.home()).resolve()
    plans, errors, legacy, other_copies = [], [], [], set()
    for process in processes:
        if Path(process['runtime']).resolve() != runtime:
            other_copies.add(process['runtime'])
        if not process['shared']:
            legacy.append(process)
    for process in sorted(legacy, key=lambda p: (p['repo_root'], p['pid'])):
        observed = process['resources']
        print(f'{process["repo_root"]}: per-session GPU MCP PID {process["pid"]}; '
              f'{observed["threads"]} threads, {observed["sockets"]} sockets. '
              'Finish active work, then quit/resume its Codex session to load the updated SSH helper.', flush=True)
    for repo in repos:
        try:
            config_file = Path(repo) / '.codex/config.toml'
            with config_file.open('rb') as stream:
                server = tomllib.load(stream).get('mcp_servers', {}).get('gpu-cluster-mcp', {})
            args = server.get('args', [])
            entries = [p for p in args if isinstance(p, str) and Path(p).name in
                       {'gpu_mcp_codex_launcher.py', 'gpu_mcp_server.py'}]
            # Existing per-session servers load the fix on the usual quit/resume.
            if server.get('enabled', True) is False or len(entries) != 1:
                continue
            installed = Path(entries[0]).resolve().parent
            if installed != runtime:
                other_copies.add(str(installed))
            if installed != runtime or not shared_entrypoint(entries[0]) or args.count('--config') != 1:
                continue
            config = Path(args[args.index('--config') + 1]).resolve()
            if config.parent != Path(repo).resolve():
                raise ValueError('registration names another repository')
            base = Path(args[args.index('--state-base') + 1]) if '--state-base' in args else home / 'gpu-mcp/state/codex'
            if not base.is_absolute():
                raise ValueError('--state-base must be absolute')
            plan = {'config': config, 'root': runtime_root(config, base=base), 'runtime': runtime,
                    'python': server['command'], 'server': server, 'registration': config_file,
                    'config_hash': hashlib.sha256(config.read_bytes()).hexdigest(),
                    'registration_hash': hashlib.sha256(config_file.read_bytes()).hexdigest()}
            observed = inspect_backend(plan)
            if observed:
                plans.append(dict(plan, **observed))
        except (OSError, ValueError, KeyError, IndexError, sqlite3.Error) as exc:
            errors.append(f'{repo}: SSH recovery check: {exc}')
    if errors:
        raise ValueError('\n'.join(errors))
    for copy in sorted(other_copies):
        print(f'Also detected GPU MCP code copy: {copy}. Keep its code updated as well; '
              'this command preserves separate installations.', flush=True)
    result = {'repaired': [], 'refreshed': [], 'deferred': [], 'preview': preview,
              'legacy_sessions': legacy, 'other_installations': sorted(other_copies)}
    for plan in plans:
        repo = str(plan['config'].parent)
        action = plan['action']
        print(f'{repo}: SSH repair: {action}; {plan["resources"]["threads"]} threads, '
              f'{plan["resources"]["sockets"]} sockets', flush=True)
        if action == 'defer_busy':
            result['deferred'].append(repo)
            continue
        if preview:
            continue
        root, info = plan['root'], plan['info']
        private_directory(root)
        with os.fdopen(os.open(root / 'startup.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'w') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            verify_process(plan, info)
            jobs = saved_jobs(plan)
            audit = {'repo_root': repo, 'pid': info['pid'], 'process_start': info['process_start'],
                     'server_instance_id': info['server_instance_id'], 'resources': plan['resources'],
                     'action': action, 'at': reservations.iso_timestamp(),
                     'preserved_jobs': [j['job_id'] for j in jobs], 'state': 'prepared'}
            audit_path = root / 'repairs' / f'ssh-{info["pid"]}-{reservations.utc_timestamp()}.json'
            reservations.atomic_write_json(audit_path, audit)
            if action == 'recover_exhausted':
                stop_exhausted(plan, info)
            else:
                answer = launcher.request(info, '/human/stop-if-idle', payload={}, human=True, timeout=5)
                if answer.get('status') != 'stopping':
                    result['deferred'].append(repo)
                    continue
                deadline = time.monotonic() + 15
                while launcher.alive(info) and time.monotonic() < deadline:
                    time.sleep(.1)
                if launcher.alive(info):
                    raise ValueError('idle GPU MCP has not stopped; no force signal was sent')
            audit.update(state='stopped')
            reservations.atomic_write_json(audit_path, audit)
        # ensure_backend takes the same startup lock; do not call it while held.
        replacement = start_backend(plan)
        audit.update(state='restarted', replacement_server_instance_id=replacement['server_instance_id'])
        reservations.atomic_write_json(audit_path, audit)
        result['repaired' if action == 'recover_exhausted' else 'refreshed'].append(repo)
        print(f'{repo}: GPU MCP restarted; saved jobs, events and reservations retained.', flush=True)
    return result
