"""Stdlib build supervisor. Only MCP's authenticated launcher calls this module.

The build interpreter and every compiler child run inside bubblewrap. Control
records and this supervisor remain outside; repository files are not authority.
"""
from __future__ import annotations

import ast
import base64
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zlib


def inside(path, roots):
    path = Path(path).resolve()
    # Roots are canonical values captured from approved policy, not aliases to
    # re-resolve after a job has changed the filesystem.
    return any(path == Path(root) or Path(root) in path.parents for root in roots)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.write('\n')
    temporary.replace(path)


def completion_hint(spec):
    """Publish presence only; a repo-writable marker never proves completion."""
    if not spec.get('ready_file'):
        return
    root = Path(spec['repo_root'])
    parts = Path(spec['ready_file']).relative_to(root).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        try:
            marker = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            os.close(marker)
        except FileExistsError:
            pass
    finally:
        os.close(fd)


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


def same_process(expected):
    return bool(expected and identity(expected['pid']) == expected)


def validate_source(source, filename):
    tree = ast.parse(source, filename)
    remote = {'asyncssh', 'fabric', 'paramiko', 'ftplib', 'telnetlib'}
    for node in ast.walk(tree):
        imports = []
        if isinstance(node, ast.Import):
            imports = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or '']
        if any(name.split('.')[0] in remote for name in imports):
            raise ValueError('remote-control imports are not build operations')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {'exec', 'eval', '__import__'}:
                raise ValueError(f'explicit dynamic execution is not allowed: {node.func.id}')


def control_directory(spec):
    root = Path(spec.get('worker_state_root') or
                Path.home() / '.local/state/gpu-mcp/build-workers').expanduser().resolve()
    if inside(root, spec['write_roots']):
        raise ValueError('build control storage overlaps an approved write root')
    for key in ('job_id', 'active_attempt_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', spec[key]):
            raise ValueError(f'invalid {key}')
    return root / spec['job_id'] / spec['active_attempt_id']


def prepare(spec, source):
    directory = control_directory(spec)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory.chmod(0o700)
    # These are trusted, captured MCP sources, not files reread from the repo.
    (directory / 'worker.py').write_text(source)
    write_json(directory / 'spec.json', spec)
    return directory


def _bind(argv, fds, path, destination, writable=False, device=False):
    path = Path(path).resolve(strict=True)
    fd = os.open(path, getattr(os, 'O_PATH', os.O_RDONLY) | os.O_CLOEXEC)
    fds.append(fd)
    argv += ['--dev-bind' if device else '--bind' if writable else '--ro-bind',
             f'/proc/self/fd/{fd}', str(destination)]


def socket_filter(directory):
    """Deny outbound connections, including pathname sockets on writable mounts.

    A network namespace alone does not isolate filesystem Unix sockets. Pipes,
    shared memory and inherited socketpairs remain available to compiler pools.
    The filter applies to native code and descendants, not only Python calls.
    """
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError('could not initialize build socket filter')
    fd = os.open(directory / 'socket-filter.bpf', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        syscall = lib.seccomp_syscall_resolve_name(b'connect')
        if syscall < 0 or lib.seccomp_rule_add(context, 0x50001, syscall, 0) < 0:
            raise RuntimeError('could not restrict host socket connections')
        if lib.seccomp_export_bpf(context, fd) < 0:
            raise RuntimeError('could not export build socket filter')
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        lib.seccomp_release(context)


def sandbox_command(spec, directory, script_source, info_fd, block_fd):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise RuntimeError('bubblewrap is required for artifact builds on this host')
    argv = [bwrap, '--unshare-user', '--unshare-pid', '--unshare-net',
            '--unshare-ipc', '--unshare-uts', '--die-with-parent', '--new-session',
            '--cap-drop', 'ALL', '--info-fd', str(info_fd), '--block-fd', str(block_fd)]
    fds = [info_fd, block_fd]
    try:
        filter_fd = socket_filter(directory)
        fds.append(filter_fd)
        argv += ['--seccomp', str(filter_fd)]
        # Preserve the host's installed environment and ordinary read access.
        # Policy determines writes; there is no library-specific dependency list.
        _bind(argv, fds, '/', '/')
        argv += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run']
        # A repository located under a private runtime directory is still an
        # input tree, not scratch. Subsequent write mounts implement its policy.
        _bind(argv, fds, spec['repo_root'], spec['repo_root'])
        for root in sorted(spec['write_roots'], key=lambda value: len(Path(value).parts)):
            if Path(root).resolve(strict=True) != Path(root):
                raise ValueError('an approved write root changed its filesystem target')
            _bind(argv, fds, root, root, writable=True)
        if spec['build_mode'] == 'gpu_assisted':
            for node in spec['gpu_devices']:
                _bind(argv, fds, node, node, device=True)
        # A read-only private bootstrap survives modifications to staged repo code.
        inner = directory / 'entry.py'
        captured = directory / 'build-source.py'
        captured.write_text(script_source)
        inner.write_text(
            'import os,sys\n'
            'sys.dont_write_bytecode=True\n'
            f'sys.path.insert(0, {str(Path(spec["script_path"]).parent)!r})\n'
            f'sys.path.insert(0, {spec["repo_root"]!r})\n'
            f'sys.argv={[spec["script_path"], *spec["args"]]!r}\n'
            f'__file__={spec["script_path"]!r}\n'
            'os.makedirs(os.environ["HOME"],exist_ok=True)\n'
            'os.makedirs(os.environ["TMPDIR"],exist_ok=True)\n'
            f'exec(compile(open("/run/gpu-mcp-source.py").read(), {spec["script_path"]!r}, "exec"), globals())\n')
        _bind(argv, fds, inner, '/run/gpu-mcp-entry.py')
        _bind(argv, fds, captured, '/run/gpu-mcp-source.py')
        argv += ['--chdir', spec['repo_root'], '--setenv', 'CUDA_VISIBLE_DEVICES',
                 spec.get('gpu_uuid', '') if spec['build_mode'] == 'gpu_assisted' else '',
                 '--setenv', 'PYTHONDONTWRITEBYTECODE', '1',
                 '--setenv', 'HOME', '/tmp/gpu-mcp-home',
                 '--setenv', 'TMPDIR', '/tmp/gpu-mcp-tmp',
                 '--setenv', 'XDG_CACHE_HOME', '/tmp/gpu-mcp-home/.cache',
                 '--setenv', 'GPU_MCP_ARTIFACT_PATH', spec['artifact_path'],
                 '--', sys.executable, '-I', '/run/gpu-mcp-entry.py']
        return argv, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def supervise(directory):
    directory = Path(directory)
    spec = json.loads((directory / 'spec.json').read_text())
    state = {'job_id': spec['job_id'], 'active_attempt_id': spec['active_attempt_id'],
             'nonce': spec['nonce'], 'supervisor': identity(os.getpid()),
             'namespace': None, 'bubblewrap': None, 'phase': 'starting'}
    write_json(directory / 'state.json', state)
    started = time.time()
    process = None
    release_fd = None
    stopped = [False]

    def stop(signum, frame):
        stopped[0] = True
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    error = None
    rc = 127
    artifact = None
    try:
        script = Path(spec['script_path'])
        if script.is_symlink() or not inside(script, spec['script_roots']):
            raise ValueError('build script is outside approved script roots or is a symlink')
        source = script.read_text()
        validate_source(source, str(script))
        info = directory / 'namespace.json'
        fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        block_fd, release_fd = os.pipe2(os.O_CLOEXEC)
        argv, fds = sandbox_command(spec, directory, source, fd, block_fd)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True,
                                       pass_fds=tuple(fds))
        finally:
            for descriptor in fds:
                os.close(descriptor)
        state['bubblewrap'] = identity(process.pid)
        # bwrap writes the host-visible PID of its namespace init before exec.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                data = json.loads(info.read_text())
                state['namespace'] = identity(data['child-pid'])
                break
            except (ValueError, KeyError):
                if process.poll() is not None:
                    break
                time.sleep(0.02)
        state['phase'] = 'running'
        if not state['namespace'] and process.poll() is None:
            raise RuntimeError('bubblewrap did not establish a tracked process namespace')
        write_json(directory / 'state.json', state)
        if process.poll() is None:
            os.write(release_fd, b'1')
        os.close(release_fd)
        release_fd = None
        rc = process.wait()
        # Losing the namespace init kills/reaps all members, including setsid helpers.
        deadline = time.monotonic() + 10
        while same_process(state['namespace']):
            if time.monotonic() >= deadline:
                raise RuntimeError('build namespace termination is unconfirmed')
            time.sleep(0.02)
        if rc == 0 and not stopped[0]:
            path = Path(spec['artifact_path'])
            if not inside(path, spec['write_roots']):
                raise ValueError('artifact escaped approved write roots')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                actual = Path(f'/proc/self/fd/{descriptor}').resolve()
                details = os.fstat(descriptor)
                if not inside(actual, spec['write_roots']) or not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                    raise ValueError('artifact must be a file or package directory under approved write roots')
                artifact = {'path': str(path), 'kind': 'directory' if stat.S_ISDIR(details.st_mode) else 'file',
                            'bytes': details.st_size if stat.S_ISREG(details.st_mode) else None,
                            'modified_ns': details.st_mtime_ns}
            finally:
                os.close(descriptor)
    except BaseException as exc:
        error = str(exc)
        print(f'BUILD ERROR: {error}', file=sys.stderr, flush=True)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
    finally:
        if release_fd is not None:
            os.close(release_fd)
    outcome = {'job_id': spec['job_id'], 'attempt_id': spec['active_attempt_id'],
               'nonce': spec['nonce'], 'exit_code': rc, 'error_summary': error,
               'started_at': started, 'ended_at': time.time(), 'artifact': artifact,
               'terminal_status': 'success' if rc == 0 and not error and not stopped[0] else 'failure'}
    write_json(directory / 'outcome.json', outcome)
    state['phase'] = 'ended'
    write_json(directory / 'state.json', state)
    try:
        completion_hint(spec)
    except OSError as exc:
        print(f'Build completion hint unavailable: {exc}', file=sys.stderr)


def inspect(spec):
    directory = control_directory(spec)
    state_file = directory / 'state.json'
    if not state_file.exists():
        state_file = directory / 'launch.json'
        if not state_file.exists():
            return {'state': 'unknown', 'reason': 'build launch has no protected state yet'}
    state = json.loads(state_file.read_text())
    if state.get('nonce') != spec['nonce']:
        raise ValueError('build attempt identity mismatch')
    alive = any(same_process(state.get(key)) for key in ('supervisor', 'bubblewrap', 'namespace'))
    outcome = None
    if not alive and (directory / 'outcome.json').exists():
        outcome = json.loads((directory / 'outcome.json').read_text())
        if outcome.get('nonce') != spec['nonce']:
            raise ValueError('build outcome identity mismatch')
    return {'state': 'alive' if alive else 'gone', 'process': state, 'outcome': outcome}


def rpc(action, spec, source=None):
    if action == 'launch':
        directory = prepare(spec, source)
        path = Path(spec['output_file'])
        if path.is_symlink() or not inside(path, spec['output_roots']):
            raise ValueError('build log is outside approved output roots or is a symlink')
        log_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            process = subprocess.Popen([sys.executable, '-I', str(directory / 'worker.py'),
                                        '--supervise', str(directory)],
                                       stdin=subprocess.DEVNULL, stdout=log_fd,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       close_fds=True)
        finally:
            os.close(log_fd)
        launched = {'job_id': spec['job_id'], 'active_attempt_id': spec['active_attempt_id'],
                    'nonce': spec['nonce'], 'supervisor': identity(process.pid)}
        write_json(directory / 'launch.json', launched)
        print(json.dumps({'status': 'launched', 'supervisor': launched['supervisor']}))
    elif action == 'status':
        print(json.dumps(inspect(spec)))
    elif action == 'stop':
        observed = inspect(spec)
        process = (observed.get('process') or {}).get('supervisor')
        if same_process(process):
            os.kill(process['pid'], signal.SIGTERM)
        else:
            # A lost supervisor must not make its tracked namespace unmanageable.
            namespace = (observed.get('process') or {}).get('namespace')
            if same_process(namespace):
                os.kill(namespace['pid'], signal.SIGKILL)
        print(json.dumps({'status': 'stop_requested', **observed}))
    else:
        raise ValueError('unknown build worker operation')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--supervise':
        supervise(sys.argv[2])
    else:
        raise SystemExit('This module is launched only by GPU MCP')
