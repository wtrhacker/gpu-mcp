"""Captured execution-host MPS supervisor. Scientific scripts never spawn cases.

Authoritative state and control requests are outside approved job write roots.
Only the MCP manager supplies specs; the public tools expose no worker command.
"""
from __future__ import annotations

import base64
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time
import traceback


def timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] in {'Z', 'X', 'x'}:
            return None
        return {'pid': int(pid), 'start': fields[19],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return None
        raise


def validate_identity(expected):
    if (not isinstance(expected, dict) or set(expected) != {'pid', 'start', 'boot'}
            or type(expected['pid']) is not int or expected['pid'] <= 0
            or not isinstance(expected['start'], str) or not expected['start'].isdigit()
            or not isinstance(expected['boot'], str)
            or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', expected['boot'])):
        raise ValueError('MPS process identity is incomplete or malformed')


def same_process(expected):
    if expected is None:
        return False
    validate_identity(expected)
    return identity(expected['pid']) == expected


def private_path(path):
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError('MPS authority must not contain symlinks')
    return path


def log_descriptor(path, flags):
    """Open a log through pinned directories; reject link swaps at any level."""
    parts = Path(path).absolute().parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return os.open(parts[-1], flags | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    finally:
        os.close(fd)


def open_log(path):
    return log_descriptor(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)


def read_log_tail(path, lines=200):
    if type(lines) is not int or not 1 <= lines <= 200:
        raise ValueError('log lines must be 1 through 200')
    with os.fdopen(log_descriptor(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('job log is not a regular file')
        start = max(0, stream.seek(0, os.SEEK_END) - 32000)
        stream.seek(start)
        data = stream.read(32000)
    if start:
        newline = data.find(b'\n')
        data = data[newline + 1:] if newline >= 0 else b''
    return ''.join(data.decode(errors='replace').splitlines(keepends=True)[-lines:])


def read_json(path):
    fd = os.open(private_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
            raise ValueError('invalid MPS control file')
        return json.load(stream)


def write_json(path, data):
    path = private_path(path)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def request_lock(path):
    fd = os.open(path / 'requests.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def directory(spec):
    root = private_path(spec.get('worker_state_root') or Path.home() / '.local/state/gpu-mcp/mps-workers')
    for output in (*spec['write_roots'], *spec.get('output_roots', [])):
        if root.resolve().is_relative_to(Path(output).resolve()):
            raise ValueError('MPS authority overlaps an approved write root')
    for key in ('job_id', 'active_attempt_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', spec[key]):
            raise ValueError('invalid MPS job identity')
    return root / spec['job_id'] / spec['active_attempt_id']


def authenticate(spec):
    path = directory(spec)
    stored = read_json(path / 'spec.json')
    for key in ('job_id', 'active_attempt_id', 'nonce', 'host', 'repo_root', 'gpu_index', 'gpu_uuid', 'server_instance_id'):
        if stored.get(key) != spec.get(key):
            raise ValueError('MPS control identity does not match its captured spec')
    return path, stored


def hint(spec, outcome=None):
    # Presence/mtime are readiness only; content is never lifecycle authority.
    try:
        path = private_path(spec['ready_file'])
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.utime(path, None, follow_symlinks=False)
        if outcome is not None:
            # This repository mirror is advisory. Shared monitoring uses the
            # readiness hint; MPS status reads protected execution-host state.
            write_json(path.with_name('outcome.json'), outcome)
    except OSError:
        pass


class MpsControl:
    def __init__(self, spec, state):
        self.spec, self.state = spec, state
        self.processes = []
        self.env = dict(os.environ, CUDA_VISIBLE_DEVICES=spec['gpu_uuid'],
                        CUDA_MPS_PIPE_DIRECTORY=spec['pipe_directory'],
                        CUDA_MPS_LOG_DIRECTORY=spec['mps_log_directory'],
                        CUDA_MPS_PROTOCOL_VERSION=str(spec['protocol']))

    def command(self, words, timeout=5):
        if self.spec.get('test_driver'):
            raise RuntimeError('test driver commands use the explicit fake control methods')
        binary = self.spec['mps_binary']
        result = subprocess.run([binary], input=' '.join(words) + '\n', env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if result.returncode or result.stdout.strip().lower().startswith(('error', 'failed')):
            raise RuntimeError((result.stderr or result.stdout or 'MPS control failed')[:500])
        return result.stdout.strip()

    def start(self):
        for key in ('pipe_directory', 'mps_log_directory'):
            path = private_path(self.spec[key])
            path.mkdir(parents=True, mode=0o700, exist_ok=False)
        if self.spec.get('test_driver'):
            for name in ('daemon', 'server'):
                proc = subprocess.Popen([sys.executable, '-I', '-c', 'import time;time.sleep(86400)'],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.state[name] = identity(proc.pid)
                self.processes.append(proc)
            return
        with (directory(self.spec) / 'mps-control.log').open('ab') as log:
            proc = subprocess.Popen([self.spec['mps_binary'], '-f'],
                env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.state['daemon'] = identity(proc.pid)
        self.processes.append(proc)
        deadline = time.monotonic() + 15
        while True:
            try:
                self.command(['get_server_list'])
                break
            except RuntimeError:
                if time.monotonic() >= deadline or not same_process(self.state['daemon']):
                    raise
                time.sleep(.1)

    def clients(self):
        if self.spec.get('test_driver'):
            return {case['process']['pid'] for case in self.state['cases']
                    if case.get('connected') and same_process(case.get('process'))}
        pids = [int(v) for v in self.command(['get_server_list']).split() if v.isdigit()]
        if len(pids) > 1:
            raise RuntimeError('private MPS endpoint has unexpected servers')
        if not pids:
            return set()
        expected = self.state.get('server')
        observed = identity(pids[0])
        if expected and observed != expected:
            raise RuntimeError('MPS server identity changed')
        self.state['server'] = observed
        return {int(v) for v in self.command(['get_client_list', str(pids[0])]).split() if v.isdigit()}

    def terminate(self, case):
        process = case.get('process')
        if not same_process(process):
            raise RuntimeError('client process identity is no longer established')
        if self.spec.get('test_driver'):
            fail = directory(self.spec) / 'test-refuse-termination'
            if fail.exists():
                raise RuntimeError('test MPS termination refused')
            result = '0'
        else:
            if process['pid'] not in self.clients():
                raise RuntimeError('client is not registered with the owned MPS server')
            if not same_process(self.state.get('server')):
                raise RuntimeError('owned MPS server identity is not established')
            result = self.command(['terminate_client', str(self.state['server']['pid']), str(process['pid'])], timeout=10)
            if result != '0':
                raise RuntimeError('MPS did not confirm CUDA context termination: ' + result[:200])
        self.state.setdefault('termination_receipts', []).append({'case_id': case['case_id'],
            'attempt_id': case['attempt_id'], 'process': process, 'at': timestamp()})
        case['gpu_cleared'] = True

    def stop(self):
        if self.spec.get('test_driver'):
            for name in ('server', 'daemon'):
                safe_signal(self.state.get(name), signal.SIGTERM)
        else:
            if self.clients():
                raise RuntimeError('MPS shutdown requires all clients gone')
            self.command(['quit'])
        deadline = time.monotonic() + 10
        while any(same_process(self.state.get(k)) for k in ('server', 'daemon')):
            if time.monotonic() >= deadline:
                raise RuntimeError('MPS service has not exited')
            time.sleep(.05)
        for proc in self.processes:
            proc.wait(timeout=1)

    def health(self):
        if not same_process(self.state.get('daemon')):
            raise RuntimeError('owned MPS controller disappeared')
        clients = self.clients()
        expected = {c['process']['pid'] for c in self.state['cases'] if c.get('process')}
        if clients - expected:
            raise RuntimeError('private MPS service contains an undeclared client')
        active = {c['process']['pid'] for c in self.state['cases']
                  if c.get('connected') and not c.get('gpu_cleared') and same_process(c.get('process'))}
        if active - clients:
            raise RuntimeError('an active CUDA client lost its MPS connection')
        if not self.spec.get('test_driver') and self.state.get('server'):
            status = self.command(['get_server_status', str(self.state['server']['pid'])])
            if 'FAULT' in status.upper():
                raise RuntimeError('MPS reported a shared GPU fault')


def pidfd_open(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    # Older Python/glibc builds on supported Linux hosts expose neither wrapper.
    # These syscall numbers are shared by x86-64 and AArch64 Linux.
    if sys.platform != 'linux' or platform.machine() not in {'x86_64', 'aarch64'}:
        raise RuntimeError('safe MPS signalling requires Linux PID handles')
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open failed')
    return fd


def pidfd_send_signal(fd, sig):
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal(fd, sig)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(424), ctypes.c_int(fd), ctypes.c_int(sig),
                          ctypes.c_void_p(), ctypes.c_uint(0))
    if result < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal failed')


def safe_signal(expected, sig):
    if not same_process(expected):
        return False
    fd = pidfd_open(expected['pid'])
    try:
        if not same_process(expected):
            return False
        pidfd_send_signal(fd, sig)
        return True
    finally:
        os.close(fd)


def initialize_cuda(expected_uuid):
    import uuid
    lib = ctypes.CDLL('libcuda.so.1')
    def call(name, *args):
        code = getattr(lib, name)(*args)
        if code:
            raise RuntimeError(f'{name} failed with CUDA code {code}')
    call('cuInit', 0)
    count = ctypes.c_int()
    call('cuDeviceGetCount', ctypes.byref(count))
    if count.value != 1:
        raise RuntimeError('MPS client must see exactly the reserved GPU')
    device = ctypes.c_int()
    call('cuDeviceGet', ctypes.byref(device), 0)
    value = (ctypes.c_ubyte * 16)()
    call('cuDeviceGetUuid', ctypes.byref(value), device)
    if 'GPU-' + str(uuid.UUID(bytes=bytes(value))) != expected_uuid:
        raise RuntimeError('CUDA initialized a different GPU')
    context = ctypes.c_void_p()
    call('cuDevicePrimaryCtxRetain', ctypes.byref(context), device)
    call('cuCtxSetCurrent', context)
    return lib, context, device


def case_main(path, case_id, attempt_id, report_fd, command_fd):
    spec = read_json(Path(path) / 'spec.json')
    case = read_json(Path(path) / ('case-' + case_id + '.json'))
    if case['attempt_id'] != attempt_id:
        raise RuntimeError('stale case attempt')
    # Report channels were opened by the trusted launcher before the guard.
    def report(data):
        try:
            os.write(report_fd, (json.dumps(data) + '\n').encode())
            return True
        except BrokenPipeError:
            return False
    def release_bootstrap():
        if cuda and cuda[0].cuDevicePrimaryCtxRelease(cuda[2]):
            while True:
                time.sleep(1)
    cuda = None
    started = False
    try:
        failure = spec.get('test_bootstrap_failure')
        if spec.get('test_driver') and (failure is True or isinstance(failure, list) and case_id in failure):
            raise RuntimeError('test CUDA bootstrap failed')
        if not spec.get('test_driver'):
            cuda = initialize_cuda(spec['gpu_uuid'])
        if not report({'phase': 'ready'}):
            release_bootstrap()
            return 125
        command = os.read(command_fd, 1)
        if command == b'A':
            # No scientific code has run: release the bootstrap's empty context
            # normally, even if CUDA fell back instead of attaching to MPS.
            release_bootstrap()
            print('GPU MCP refused CUDA fallback: client did not attach to the private MPS server.', file=sys.stderr)
            report({'phase': 'aborted', 'exit_code': 126})
            return 126
        if command != b'R':
            release_bootstrap()
            return 125
        started = True
        script = Path(case['script_path'])
        module_spec = importlib.util.spec_from_file_location('captured_mps_guard', Path(path) / 'guard.py')
        guard = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(guard)
        source = guard.read_script_no_follow(script, script_roots=[Path(p) for p in spec['script_roots']]).encode()
        if hashlib.sha256(source).hexdigest() != case['script_hash']:
            raise RuntimeError('declared script changed; request a new explicit attempt')
        code = 0
        try:
            code = guard.run_job(str(script), case['args'], repo_root=Path(spec['repo_root']),
                script_roots=[Path(p) for p in spec['script_roots']],
                write_roots=[Path(p) for p in spec['write_roots']], captured_source=source.decode())
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except BaseException:
            traceback.print_exc()
            code = 1
        report({'phase': 'done', 'exit_code': code})
        # Even exceptions wait for the parent to clear all CUDA contexts.
        if os.read(command_fd, 1) != b'E':
            while True:
                time.sleep(1)
        return code
    except BaseException:
        traceback.print_exc()
        if not started:
            release_bootstrap()
            report({'phase': 'aborted', 'exit_code': 126})
            return 126
        report({'phase': 'done', 'exit_code': 126})
        # Never exit a possibly attached CUDA client without its GPU teardown.
        while os.read(command_fd, 1) != b'E':
            time.sleep(.1)
        return 126


def new_case(entry):
    return dict(entry, state='waiting', attempt_id='case-attempt-' + os.urandom(8).hex(),
                process=None, connected=False, attempts=[])


def supervise(path):
    path = Path(path)
    spec = read_json(path / 'spec.json')
    state = {'job_id': spec['job_id'], 'active_attempt_id': spec['active_attempt_id'], 'nonce': spec['nonce'],
             'parent': identity(os.getpid()), 'cases': [new_case(c) for c in spec['runs']],
             'state': 'running', 'paused': False, 'requests_applied': [], 'started_at': timestamp(),
             'service_launching': False, 'service_attempted': False}
    control = MpsControl(spec, state)
    running = {}
    stopping = [False]
    health_after = 0
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__(0, True))
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__(0, True))

    def save():
        write_json(path / 'state.json', state)
        hint(spec, state.get('outcome'))

    def finish_case(case, code):
        item = running.pop(case['case_id'])
        item['proc'].wait()
        for fd in ('report', 'command'):
            os.close(item[fd])
        case['exit_code'] = code
        case['ended_at'] = timestamp()
        case['state'] = 'cancelled' if case.get('stop_requested') else 'succeeded' if code == 0 else 'failed'
        case['attempts'].append({k: case.get(k) for k in ('attempt_id', 'state', 'process', 'exit_code', 'output_file', 'script_hash', 'started_at', 'ended_at')})
        save()

    def cancel_waiting(case):
        case.update(state='cancelled', ended_at=timestamp(), gpu_cleared=True, stop_requested=True)
        if not any(a['attempt_id'] == case['attempt_id'] for a in case['attempts']):
            case['attempts'].append({k:case.get(k) for k in ('attempt_id','state','process','output_file','script_hash','ended_at')})

    try:
        save()
        fd = pidfd_open(os.getpid())
        os.close(fd)  # Refuse unsupported kernels before creating a service.
        state.update(service_launching=True, service_attempted=True)
        save()
        control.start()
        state['service_launching'] = False
        save()
        while True:
            if time.monotonic() >= health_after:
                try:
                    control.health()
                except (OSError, RuntimeError) as exc:
                    state.update(paused=True, reason=str(exc))
                    stopping[0] = True
                    save()
                health_after = time.monotonic() + 1
            request_file = path / 'requests.json'
            requests = read_json(request_file) if request_file.exists() else []
            for request in requests:
                if request['request_id'] in state['requests_applied']:
                    continue
                case = next((c for c in state['cases'] if c['case_id'] == request.get('case_id')), None)
                if request['action'] == 'stop' and case is None:
                    stopping[0] = True
                elif case is not None and case['attempt_id'] == request['expected_attempt']:
                    if request['action'] == 'stop':
                        if case['state'] == 'waiting':
                            cancel_waiting(case)
                        elif case['case_id'] in running:
                            case['stop_requested'] = True
                    elif request['action'] == 'retry' and case['state'] in {'succeeded', 'failed', 'cancelled'}:
                        if same_process(case.get('process')) or not case.get('gpu_cleared', case.get('process') is None):
                            raise RuntimeError('prior case is not safely gone')
                        for key in ('pending_exit', 'exit_code', 'ended_at', 'started_at'):
                            case.pop(key, None)
                        case.update(request['replacement'], state='waiting', process=None, connected=False,
                                    gpu_cleared=False, stop_requested=False)
                state['requests_applied'].append(request['request_id'])
                save()
            if stopping[0]:
                for case in state['cases']:
                    if case['state'] == 'waiting':
                        cancel_waiting(case)
                    elif case['case_id'] in running:
                        case['stop_requested'] = True
            for case in state['cases']:
                item = running.get(case['case_id'])
                if not item:
                    continue
                ready, _, _ = select.select([item['report']], [], [], 0)
                if ready:
                    chunk = os.read(item['report'], 65536)
                    item['buffer'] += chunk
                    while b'\n' in item['buffer']:
                        line, item['buffer'] = item['buffer'].split(b'\n', 1)
                        message = json.loads(line)
                        if message['phase'] == 'ready':
                            if spec.get('test_driver'):
                                case['connected'] = True
                            else:
                                case['connected'] = case['process']['pid'] in control.clients()
                            if not case['connected']:
                                case['pending_exit'] = 126
                                os.write(item['command'], b'A')
                                save()
                                continue
                            case['state'] = 'running'
                            os.write(item['command'], b'R')
                            save()
                        elif message['phase'] == 'aborted':
                            case.update(gpu_cleared=True, pending_exit=126)
                            save()
                        elif message['phase'] in {'done', 'bootstrap_failed'}:
                            case['pending_exit'] = message.get('exit_code', 126)
                if ((case.get('pending_exit') is not None or case.get('stop_requested'))
                        and case.get('connected') and not case.get('gpu_cleared')):
                    try:
                        control.terminate(case)
                        save()  # Durable CUDA-clearance receipt precedes an OS signal or exit permission.
                        if case.get('stop_requested'):
                            safe_signal(case['process'], signal.SIGKILL)
                        else:
                            os.write(item['command'], b'E')
                        save()
                    except (RuntimeError, OSError) as exc:
                        state.update(paused=True, reason=str(exc))
                        save()
                rc = item['proc'].poll()
                if rc is not None:
                    if not ready:
                        continue  # Consume the final pipe report before classifying an exit.
                    if not case.get('gpu_cleared'):
                        state.update(paused=True, reason='client exited without confirmed CUDA teardown')
                        stopping[0] = True
                    finish_case(case, rc if rc else case.get('pending_exit', rc))
            if not stopping[0] and not state['paused']:
                for case in state['cases']:
                    if len(running) >= spec['max_concurrent']:
                        break
                    if case['state'] != 'waiting':
                        continue
                    case.update(state='launching', started_at=timestamp(), gpu_cleared=False)
                    save()  # Launch intent precedes Popen and its acknowledgement.
                    write_json(path / ('case-' + case['case_id'] + '.json'), case)
                    report_r, report_w = os.pipe()
                    command_r, command_w = os.pipe()
                    env = dict(os.environ, **spec['job_env'], CUDA_MPS_PIPE_DIRECTORY=state.get('client_pipe', spec['pipe_directory']),
                               CUDA_MPS_PROTOCOL_VERSION=str(spec['protocol']),
                               GPU_MCP_PROCESS_FINGERPRINT=spec['nonce'] + ':' + case['attempt_id'])
                    # UUID masks avoid physical-ordinal remapping and keep NVML
                    # framework discovery consistent with the one-GPU server.
                    env['CUDA_VISIBLE_DEVICES'] = spec['gpu_uuid']
                    log_fd = open_log(case['output_file'])
                    try:
                        proc = subprocess.Popen([sys.executable, '-I', str(path / 'worker.py'), '--case', str(path),
                            case['case_id'], case['attempt_id'], str(report_w), str(command_r)],
                            env=env, cwd=spec['repo_root'], stdin=subprocess.DEVNULL, stdout=log_fd,
                            stderr=subprocess.STDOUT, pass_fds=(report_w, command_r), start_new_session=True)
                    finally:
                        os.close(log_fd)
                        os.close(report_w)
                        os.close(command_r)
                    case['process'] = identity(proc.pid)
                    running[case['case_id']] = {'proc': proc, 'report': report_r, 'command': command_w, 'buffer': b''}
                    save()
            if not running and not any(c['state'] in {'waiting', 'launching', 'running'} for c in state['cases']):
                with request_lock(path):
                    queue = read_json(request_file) if request_file.exists() else []
                    if any(r['request_id'] not in state['requests_applied'] for r in queue):
                        continue
                    state['state'] = 'closing'
                    save()  # Close admission atomically before shutting MPS down.
                control.stop()
                succeeded = all(c['state'] == 'succeeded' for c in state['cases']) and not state.get('reason')
                state.update(state='gone', ended_at=timestamp(), outcome={'schema_version': 1,
                    'job_id': spec['job_id'], 'attempt_id': spec['active_attempt_id'],
                    'host': spec['host'], 'gpu_index': spec['gpu_index'], 'reservation_key': spec['reservation_key'],
                    'remote_pid': state['parent']['pid'], 'terminal_status': 'success' if succeeded else 'failure',
                    'exit_code': 0 if succeeded else 1, 'started_at': state['started_at'], 'ended_at': timestamp(),
                    'error_summary': state.get('reason') or ('' if succeeded else 'Sweep finished with failed or cancelled runs.')})
                save()
                return
            time.sleep(.05)
    except BaseException as exc:
        state.update(paused=True, reason=str(exc), state='unknown')
        if not running and all(c['state'] == 'waiting' for c in state['cases']) and state.get('daemon'):
            try:
                if not control.clients():
                    control.stop()
                    state['service_launching'] = False
                    for case in state['cases']:
                        cancel_waiting(case)
            except (RuntimeError, OSError):
                pass  # Keep the captured service identity and reservation.
        save()
        # A crashed parent is not permission to signal clients or free its GPU.
        traceback.print_exc()


def validate_outcome(spec, state):
    outcome = state['outcome']
    for key in ('job_id', 'host', 'gpu_index', 'reservation_key'):
        if outcome.get(key) != spec.get(key):
            raise ValueError('MPS completion identity does not match ' + key)
    if (outcome.get('schema_version') != 1 or outcome.get('attempt_id') != spec['active_attempt_id']
            or outcome.get('remote_pid') != state['parent']['pid']
            or outcome.get('terminal_status') not in {'success', 'failure'}
            or type(outcome.get('exit_code')) is not int
            or not outcome.get('ended_at') or not outcome.get('started_at')):
        raise ValueError('MPS completion is incomplete or inconsistent')
    success = outcome['terminal_status'] == 'success'
    if success != (outcome['exit_code'] == 0) or success and not all(c['state'] == 'succeeded' for c in state['cases']):
        raise ValueError('MPS aggregate result conflicts with case outcomes')


def inspect(spec):
    path, stored = authenticate(spec)
    state_file = path / 'state.json'
    if not state_file.exists():
        return {'state': 'unknown', 'reason': 'MPS supervisor acknowledgement is not visible'}
    state = read_json(state_file)
    if (state.get('nonce') != stored['nonce'] or state.get('active_attempt_id') != stored['active_attempt_id']
            or state.get('job_id') != stored['job_id']):
        raise ValueError('MPS outcome identity changed')
    expected_ids = {c['case_id'] for c in stored['runs']}
    if not isinstance(state.get('cases'), list) or {c.get('case_id') for c in state['cases']} != expected_ids or len(state['cases']) != len(expected_ids):
        raise ValueError('MPS case records are missing or malformed')
    for case in state['cases']:
        if case.get('state') not in {'waiting', 'launching', 'running', 'succeeded', 'failed', 'cancelled'}:
            raise ValueError('invalid MPS case lifecycle')
        if not re.fullmatch(r'case-attempt-[a-f0-9]+', case.get('attempt_id', '')):
            raise ValueError('invalid MPS case attempt')
        if case['state'] in {'running', 'succeeded', 'failed'} and not case.get('process'):
            raise ValueError('MPS case process identity is missing')
    if not state.get('parent'):
        raise ValueError('MPS parent identity is missing')
    validate_identity(state['parent'])
    if state.get('service_attempted', True) and not state.get('service_launching') and not state.get('daemon'):
        raise ValueError('MPS service process identity is missing')
    if any(c.get('connected') for c in state['cases']) and not state.get('server'):
        raise ValueError('MPS CUDA server identity is missing')
    processes = [state.get(k) for k in ('parent', 'server', 'daemon')]
    processes += [c.get('process') for c in state['cases']]
    alive = [p for p in processes if same_process(p)]
    pending = any(c['state'] == 'launching' and not c.get('process') for c in state['cases'])
    pending = pending or state.get('service_launching', True)
    pending = pending or any(c.get('process') and not c.get('gpu_cleared') for c in state['cases']) and not state.get('server')
    if state.get('outcome'):
        validate_outcome(stored, state)
    if not alive and not pending:
        observed = 'gone'
    elif same_process(state.get('parent')) and state.get('state') == 'running':
        observed = 'alive'
    else:
        observed = 'unknown'
    return dict(state, state=observed, live_processes=alive,
                reason=state.get('reason') or ('parent is gone; clients or service may remain' if observed == 'unknown' else ''))


def cleanup_orphan(spec, state):
    """Explicit owning-server group stop after its captured supervisor died."""
    startup_failed = state.get('service_launching') and state.get('daemon') and all(
        c['state'] == 'waiting' for c in state['cases'])
    if same_process(state['parent']) or state.get('service_launching') and not startup_failed or any(
            c['state'] == 'launching' and not c.get('process') for c in state['cases']):
        raise RuntimeError('cannot establish the complete orphan group')
    control = MpsControl(spec, state)
    if startup_failed:
        if control.clients():
            raise RuntimeError('startup failed with untracked MPS clients')
        state['service_launching'] = False
    for case in state['cases']:
        if same_process(case.get('process')):
            if not case.get('gpu_cleared'):
                control.terminate(case)
                write_json(directory(spec) / 'state.json', state)
            safe_signal(case['process'], signal.SIGKILL)
        if case['state'] in {'waiting', 'launching', 'running'}:
            case.update(state='cancelled', ended_at=timestamp())
    deadline = time.monotonic() + 5
    while any(same_process(c.get('process')) for c in state['cases']):
        if time.monotonic() > deadline:
            raise RuntimeError('orphan clients did not exit')
        time.sleep(.05)
    if any(same_process(state.get(k)) for k in ('server', 'daemon')):
        control.stop()
    state.update(state='gone', paused=False, reason='Explicitly stopped after the supervisor disappeared.')
    write_json(directory(spec) / 'state.json', state)
    hint(spec)




def rpc(action, spec, sources=None, request=None):
    if action == 'preflight':
        fd = pidfd_open(os.getpid())
        try:
            pidfd_send_signal(fd, 0)
        finally:
            os.close(fd)
        return {'pid_handles': True}
    if action == 'inspect_reference':
        # The shared registry carries identity/location only, never arguments
        # or control credentials. This reference grants no mutation authority.
        path = directory(dict(spec, write_roots=[]))
        stored = read_json(path / 'spec.json')
        for key in ('job_id','active_attempt_id','host','gpu_index','reservation_key','repo_root','server_instance_id'):
            if spec.get(key) != stored.get(key):
                raise ValueError('MPS inspection reference changed')
        return inspect(stored)
    if action == 'launch':
        path = directory(spec)
        spec = dict(spec, mps_log_directory=str(path / 'mps-logs'))
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
        write_json(path / 'spec.json', spec)
        (path / 'worker.py').write_text(sources['worker'])
        (path / 'guard.py').write_text(sources['guard'])
        log_fd = open_log(spec['output_file'])
        try:
            proc = subprocess.Popen([sys.executable, '-I', str(path / 'worker.py'), '--supervise', str(path)],
                stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            os.close(log_fd)
        write_json(path / 'launch.json', {'parent': identity(proc.pid)})
        return {'status': 'launched', 'parent': identity(proc.pid)}
    path, stored = authenticate(spec)
    if action == 'logs':
        request = request or {}
        case_id = request.get('case_id')
        selected = stored
        if case_id is not None:
            selected = next((c for c in read_json(path / 'state.json')['cases'] if c['case_id'] == case_id), None)
            if selected is None:
                raise ValueError('case unavailable in this sweep')
            if selected['attempt_id'] != request.get('case_attempt_id'):
                raise ValueError('case attempt changed; refresh its status before reading the log')
        # Only saved paths are accepted; callers cannot request arbitrary files.
        return {'tail': read_log_tail(selected['output_file'], request.get('lines', 200))}
    if action == 'status':
        return inspect(stored)
    if action not in {'stop', 'retry'}:
        raise ValueError('invalid MPS control action')
    request = dict(request or {}, action=action)
    with request_lock(path):
        state = inspect(stored)
        if state['state'] != 'alive':
            if action != 'stop' or request.get('case_id') is not None or same_process(state.get('parent')):
                raise ValueError('only the live owning supervisor may process case controls')
            cleanup_orphan(stored, state)
            return {'status': 'stopped', 'request_id': request['request_id']}
        if request.get('case_id') is not None:
            case = next((c for c in state['cases'] if c['case_id'] == request['case_id']), None)
            if case is None or case['attempt_id'] != request['expected_attempt']:
                raise ValueError('case attempt changed before the request')
            if action == 'retry' and (case['state'] not in {'succeeded', 'failed', 'cancelled'}
                    or same_process(case.get('process')) or not case.get('gpu_cleared', case.get('process') is None)):
                raise ValueError('retry requires the prior case safely gone')
        file = path / 'requests.json'
        queue = read_json(file) if file.exists() else []
        if not any(r['request_id'] == request['request_id'] for r in queue):
            queue.append(request)
            write_json(file, queue)
    return {'status': 'queued', 'request_id': request['request_id']}


if __name__ == '__main__':
    if sys.argv[1] == '--supervise':
        supervise(sys.argv[2])
    elif sys.argv[1] == '--case':
        raise SystemExit(case_main(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6])))
    else:
        raise SystemExit('MPS worker is invoked only by GPU MCP')
