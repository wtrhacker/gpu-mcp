from __future__ import annotations

"""Shared stdlib guard logic for GPU MCP Python job execution."""

import ast
import atexit
import builtins
import fcntl
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import stat
import sys
import tempfile
import time
from pathlib import Path


FORBIDDEN_CALLS = {
    "os.system",
    "os.popen",
    "os.fork",
    "os.forkpty",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.posix_spawn",
    "os.posix_spawnp",
    "pty.spawn",
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
    "subprocess.Popen",
    "shutil.rmtree",
    "shutil.move",
    "shutil.chown",
    "os.dup",
    "os.dup2",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.removedirs",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.truncate",
    "os.chmod",
    "os.chown",
    "os.lchmod",
    "os.lchown",
    "os.symlink",
    "os.link",
    "Path.unlink",
    "Path.rmdir",
    "Path.rename",
    "Path.replace",
    "Path.chmod",
    "Path.lchmod",
    "Path.symlink_to",
    "Path.hardlink_to",
}

FORBIDDEN_CALL_ROOTS = {
    "subprocess",
    "pty",
}
FORBIDDEN_IMPORT_ROOTS = {
    "_io",
    "asyncssh",
    "ctypes",
    "fabric",
    "ftplib",
    "invoke",
    "paramiko",
    "posix",
    "pty",
    "subprocess",
    "telnetlib",
}
# Normal ML libraries may import ctypes internally while loading native
# extensions. Direct user-script imports are still blocked by the static scan.
RUNTIME_FORBIDDEN_IMPORT_ROOTS = FORBIDDEN_IMPORT_ROOTS - {"ctypes", "subprocess"}
FORBIDDEN_DYNAMIC_CALLS = {
    "__import__",
    "builtins.__import__",
    "builtins.compile",
    "builtins.eval",
    "builtins.exec",
    "compile",
    "eval",
    "exec",
    "getattr",
    "setattr",
    "delattr",
    "importlib.import_module",
}
FORBIDDEN_DESTRUCTIVE_METHOD_NAMES = {
    "chmod",
    "hardlink_to",
    "lchmod",
    "rmdir",
    "symlink_to",
    "unlink",
}
FORBIDDEN_COMMAND_NAMES = {
    "chown",
    "codex",
    "dd",
    "rm",
    "chmod",
    "chgrp",
    "cp",
    "curl",
    "git",
    "mkfs",
    "mv",
    "reboot",
    "rmdir",
    "rsync",
    "scp",
    "sftp",
    "shred",
    "shutdown",
    "ssh",
    "sudo",
    "su",
    "truncate",
    "umount",
    "unlink",
    "wget",
    "--dangerously-bypass-approvals-and-sandbox",
    "--ignore-rules",
    "--yolo",
}
FORBIDDEN_AUDIT_EVENTS = {
    "os.chmod",
    "os.chown",
    "os.dup",
    "os.dup2",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.link",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.symlink",
    "os.system",
    "os.truncate",
    "shutil.chown",
    "shutil.move",
    "shutil.rmtree",
    "socket.bind",
    "socket.connect",
    "socket.connect_ex",
    "subprocess.Popen",
}

def path_under_roots(path: object, roots: list[Path]) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return True
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except Exception:
        return False
    root_paths = [root.expanduser().resolve(strict=False) for root in roots]
    return any(resolved == root or root in resolved.parents for root in root_paths)


def resolve_under(
    path: str,
    *,
    repo_root: Path,
    roots: list[Path],
    label: str,
    must_exist: bool,
) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = repo_root / p
    if must_exist and not p.exists():
        raise ValueError(f"{label} does not exist: {path}")
    resolved = p.resolve(strict=must_exist)
    root_paths = [root.expanduser().resolve() for root in roots]
    if not any(resolved == root or root in resolved.parents for root in root_paths):
        allowed = ", ".join(str(root) for root in root_paths)
        raise ValueError(f"{label} must be under approved roots: {allowed}")
    return resolved


def validate_python_script_path(
    script_path: str,
    *,
    repo_root: Path,
    script_roots: list[Path],
) -> Path:
    requested = Path(script_path).expanduser()
    if not requested.is_absolute():
        requested = repo_root / requested
    if requested.is_symlink():
        raise ValueError("script_path must not be a symlink")
    script = resolve_under(
        script_path,
        repo_root=repo_root,
        roots=script_roots,
        label="script_path",
        must_exist=True,
    )
    if script.suffix != ".py":
        raise ValueError("script_path must point to a .py file")
    if not script.is_file():
        raise ValueError("script_path must point to a regular file")
    return script


def _full_call_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _full_call_name(node.value, aliases)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _module_root(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _import_rejection_issue(module_name: str) -> str | None:
    if _module_root(module_name) in FORBIDDEN_IMPORT_ROOTS:
        return f"forbidden import: {module_name}"
    return None


def _runtime_import_rejection_issue(module_name: str) -> str | None:
    if _module_root(module_name) in RUNTIME_FORBIDDEN_IMPORT_ROOTS:
        return f"forbidden runtime import: {module_name}"
    return None


def _call_rejection_issue(call_name: str | None) -> str | None:
    if not call_name:
        return None
    root = _module_root(call_name)
    leaf = call_name.rsplit(".", 1)[-1]
    if call_name in FORBIDDEN_CALLS:
        return f"forbidden call: {call_name}"
    if root in FORBIDDEN_CALL_ROOTS:
        return f"forbidden call family: {root}"
    if call_name in FORBIDDEN_DYNAMIC_CALLS or leaf in FORBIDDEN_DYNAMIC_CALLS:
        return f"forbidden dynamic execution: {call_name}"
    return None


def _literal_string_list(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return shlex.split(node.value)
        except ValueError:
            return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        values = []
        for elt in node.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                return None
            values.append(elt.value)
        return values
    return None


def _is_recursive_remove(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "rm":
        return False
    for token in tokens[1:]:
        if token == "--recursive":
            return True
        if token.startswith("-") and "r" in token.lower()[1:]:
            return True
    return False


def _dangerous_command_issue(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    command = Path(tokens[0]).name
    normalized = [command] + tokens[1:]
    if _is_recursive_remove(normalized):
        return "recursive remove command"
    for token in tokens:
        cleaned = token.strip(" \t\r\n;|&(){}[]<>")
        if cleaned and Path(cleaned).name in FORBIDDEN_COMMAND_NAMES:
            return f"forbidden command token: {Path(cleaned).name}"
    return None


def _script_rejection_message(script: Path, issues: list[str]) -> str:
    details = "\n".join(f"- {issue}" for issue in issues)
    return (
        f"REJECTED: unsafe Python GPU script: {script}\n"
        f"{details}\n"
        "This file contains destructive, shell, dynamic-execution, or remote-control "
        "behavior. Running it through GPU MCP is blocked."
    )


def read_script_no_follow(script: Path, *, script_roots: list[Path]) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(script, flags)
    try:
        fd_path = Path(f"/proc/self/fd/{fd}")
        if fd_path.exists() and not path_under_roots(fd_path.resolve(), script_roots):
            raise PermissionError(f"GPU MCP blocked script outside approved roots: {script}")
        with os.fdopen(fd, "r") as handle:
            return handle.read()
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def scan_python_source_safety(script: Path, source: str) -> list[str]:
    try:
        tree = ast.parse(source, filename=str(script))
    except SyntaxError as e:
        return [f"line {e.lineno}: invalid Python syntax: {e.msg}"]
    except UnicodeDecodeError as e:
        return [f"could not decode script as text: {e}"]

    aliases = _import_aliases(tree)
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            lineno = getattr(node, "lineno", "?")
            for item in node.names:
                issue = _import_rejection_issue(item.name)
                if issue:
                    issues.append(f"line {lineno}: {issue}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            lineno = getattr(node, "lineno", "?")
            issue = _import_rejection_issue(node.module)
            if issue:
                issues.append(f"line {lineno}: {issue}")
        elif isinstance(node, ast.Call):
            name = _full_call_name(node.func, aliases)
            lineno = getattr(node, "lineno", "?")
            issue = _call_rejection_issue(name)
            if issue is None and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_DESTRUCTIVE_METHOD_NAMES:
                    issue = f"forbidden destructive method: {node.func.attr}"
            if issue:
                issues.append(f"line {lineno}: {issue}")
            for arg in node.args:
                tokens = _literal_string_list(arg)
                if tokens:
                    issue = _dangerous_command_issue(tokens)
                    if issue:
                        issues.append(f"line {lineno}: {issue}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                tokens = shlex.split(node.value)
            except ValueError:
                tokens = [node.value]
            issue = _dangerous_command_issue(tokens)
            if issue:
                lineno = getattr(node, "lineno", "?")
                issues.append(f"line {lineno}: {issue}")
    return sorted(set(issues))


def _open_is_write(mode: object, flags: object) -> bool:
    mode_text = "" if mode is None else str(mode).lower()
    if set(mode_text) & {"w", "a", "x", "+"}:
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    return False


def _write_path_allowed(path: object, roots: list[Path]) -> bool:
    # The standard Linux null device discards output; it is not a data write
    # outside the project. Torch/dill legitimately probes it with r+b mode.
    if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == "/dev/null":
        try:
            device = os.stat(path, follow_symlinks=False)
            if stat.S_ISCHR(device.st_mode) and device.st_rdev == os.makedev(1, 3):
                return True
        except OSError:
            pass
    return path_under_roots(path, roots)


def _temporary_path(path: object, dir_fd: object = None) -> Path | None:
    """Resolve a deletion/rename entry without following its final symlink.

    shutil.rmtree uses directory descriptors for symlink-safe cleanup. Resolve
    those descriptors too; checking only the relative filename would allow an
    outside directory to be mistaken for the current working directory.
    """
    if not isinstance(path, (str, bytes, os.PathLike)):
        return None
    try:
        candidate = Path(os.fsdecode(path))
        if not candidate.is_absolute():
            if dir_fd in (None, -1):
                base = Path.cwd()
            else:
                if not isinstance(dir_fd, int) or not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                    return None
                base = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
                if not base.is_absolute():
                    return None
            candidate = base / candidate
        if candidate.name in ("", ".", ".."):
            return candidate.resolve(strict=False)
        return candidate.parent.resolve(strict=False) / candidate.name
    except (OSError, TypeError, ValueError, RuntimeError):
        return None


def _temporary_operation_allowed(event: str, args: tuple, temporary_root: Path | None) -> bool:
    if temporary_root is None:
        return False

    def inside(path: object, dir_fd: object = None) -> bool:
        resolved = _temporary_path(path, dir_fd)
        return resolved is not None and resolved.is_relative_to(temporary_root)

    if event in {"os.remove", "os.rmdir", "shutil.rmtree"} and args:
        return inside(args[0], args[1] if len(args) > 1 else None)
    if event == "os.rename" and len(args) >= 2:
        return (inside(args[0], args[2] if len(args) > 2 else None)
                and inside(args[1], args[3] if len(args) > 3 else None))
    return False


TEMPORARY_CONTAINER = ".gpu_mcp_tmp"
TEMPORARY_OWNER_FILE = ".owner.json"
TEMPORARY_RETENTION_SEC = 7 * 24 * 60 * 60


def _temporary_process_start(pid: int) -> str | None:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return None if fields[0] in {"Z", "X"} else fields[19]


def _temporary_owner_gone(owner: dict) -> bool:
    if owner.get("kind") not in {'job', 'migrated'} or owner.get("host") != socket.gethostname():
        return False
    if (type(owner.get("pid")) is not int or owner['pid'] <= 0
            or not isinstance(owner.get('process_start'), str) or not owner['process_start'].isdigit()
            or not isinstance(owner.get('boot_id'), str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', owner['boot_id'])):
        return False
    try:
        # Never infer death from editable start/boot fields. A live reused PID
        # is conservatively retained; the host and PID are bound to the path.
        return _temporary_process_start(owner['pid']) is None
    except FileNotFoundError as exc:
        return exc.filename == f"/proc/{owner['pid']}/stat"
    except (OSError, ValueError, IndexError):
        return False


def _open_private_temporary_directory(parent: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError as exc:
        raise PermissionError('GPU MCP temporary container must be a real directory owned by this user') from exc
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise PermissionError('GPU MCP temporary container belongs to another user')
    return fd


def _open_temporary_container(root: Path) -> tuple[Path, int]:
    host = socket.gethostname()
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', host) or host in {'.', '..'}:
        raise ValueError('invalid execution host identity')
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    container = None
    try:
        container = _open_private_temporary_directory(parent, TEMPORARY_CONTAINER)
        child = _open_private_temporary_directory(container, host)
    finally:
        if container is not None:
            os.close(container)
        os.close(parent)
    expected = root / TEMPORARY_CONTAINER / host
    if Path(os.readlink(f'/proc/self/fd/{child}')) != expected:
        os.close(child)
        raise PermissionError('GPU MCP temporary container identity changed')
    return expected, child


def _temporary_container(root: Path) -> Path:
    path, fd = _open_temporary_container(root)
    os.close(fd)
    return path


def _remove_temporary_contents(directory: int) -> None:
    """Walk the selected inode through FDs, never reopen a replacement path."""
    for name in os.listdir(directory):
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                identity = os.fstat(child)
                if (entry.st_dev, entry.st_ino) != (identity.st_dev, identity.st_ino):
                    continue
                _remove_temporary_contents(child)
                latest = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (latest.st_dev, latest.st_ino) == (identity.st_dev, identity.st_ino):
                    os.rmdir(name, dir_fd=directory)  # Can remove only an empty directory.
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=directory)  # Unlinks a symlink itself, never its target.


def _remove_temporary_directory(parent: int, directory: int, name: str) -> bool:
    identity = os.fstat(directory)
    latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (latest.st_dev, latest.st_ino) != (identity.st_dev, identity.st_ino):
        return False
    _remove_temporary_contents(directory)
    latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (latest.st_dev, latest.st_ino) != (identity.st_dev, identity.st_ino):
        return False
    os.rmdir(name, dir_fd=parent)
    return True


def _prune_temporary_roots(container: Path, *, now: float | None = None, parent_fd: int | None = None) -> list[str]:
    """Remove expired, unlocked scratch with process proof; run before job guards."""
    now = time.time() if now is None else now
    removed = []
    if container.name != socket.gethostname() or container.parent.name != TEMPORARY_CONTAINER:
        return removed
    parent = parent_fd if parent_fd is not None else os.open(container, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if Path(os.readlink(f'/proc/self/fd/{parent}')) != container:
            return removed
        for name in os.listdir(parent):
            matched = re.fullmatch(r'\.gpu-mcp-tmp-(\d+)-[A-Za-z0-9_-]+', name)
            if matched is None:
                continue
            directory = owner_fd = None
            try:
                directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                identity = os.fstat(directory)
                if identity.st_uid != os.getuid():
                    continue
                owner_fd = os.open(TEMPORARY_OWNER_FILE, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                owner_info = os.fstat(owner_fd)
                if not stat.S_ISREG(owner_info.st_mode) or owner_info.st_uid != os.getuid() or owner_info.st_size > 4096:
                    continue
                fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owner = json.loads(os.read(owner_fd, 4096))
                if not isinstance(owner, dict) or owner.get('schema_version') != 1:
                    continue
                if str(owner.get('pid')) != matched[1]:
                    continue
                ended = owner.get('finished_at', owner.get('created_at'))
                if (type(ended) not in {int, float} or not math.isfinite(ended) or ended <= 0
                        or now - ended < TEMPORARY_RETENTION_SEC or not _temporary_owner_gone(owner)):
                    continue
                latest = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (latest.st_dev, latest.st_ino) != (identity.st_dev, identity.st_ino):
                    continue
                if _remove_temporary_directory(parent, directory, name):
                    removed.append(name)
            except (OSError, ValueError, TypeError, OverflowError):
                continue  # Uncertain ownership and busy folders stay intact.
            finally:
                if owner_fd is not None:
                    os.close(owner_fd)
                if directory is not None:
                    os.close(directory)
    finally:
        if parent_fd is None:
            os.close(parent)
    return removed


def _prepare_temporary_root(write_roots: list[Path]) -> tuple[Path | None, dict | None, dict | None]:
    """Create private per-job scratch beneath one container in an approved root."""
    if not write_roots:
        return None, None, None
    root = write_roots[0].expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    container, parent_fd = _open_temporary_container(root)
    directory_fd = owner_fd = None
    try:
        _prune_temporary_roots(container, parent_fd=parent_fd)
        if Path(os.readlink(f'/proc/self/fd/{parent_fd}')) != container:
            raise PermissionError('GPU MCP temporary container identity changed before creation')
        name = f'.gpu-mcp-tmp-{os.getpid()}-{secrets.token_hex(8)}'
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        temporary_root = Path(os.readlink(f'/proc/self/fd/{directory_fd}'))
        if temporary_root != container / name:
            raise PermissionError('GPU MCP scratch identity changed before initialization')
        owner_fd = os.open(TEMPORARY_OWNER_FILE, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=directory_fd)
    except BaseException:
        for fd in (owner_fd, directory_fd, parent_fd):
            if fd is not None:
                os.close(fd)
        raise
    owner = {'schema_version': 1, 'kind': 'job', 'host': socket.gethostname(), 'pid': os.getpid(),
             'process_start': _temporary_process_start(os.getpid()),
             'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'created_at': time.time()}
    fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(owner_fd, json.dumps(owner).encode())
    os.environ["GPU_MCP_TEMP_ROOT"] = str(temporary_root)
    os.environ["TMPDIR"] = str(temporary_root)
    # tempfile may have cached /tmp before the guard was installed.
    tempfile.tempdir = str(temporary_root)
    handles = {'parent': parent_fd, 'directory': directory_fd, 'owner': owner_fd}
    return temporary_root, owner, handles


def make_safe_run_audit_hook(write_roots: list[Path], *, temporary_root: Path | None = None):
    # Capture the directory once. An environment change never grants more
    # permissions, and replacing a path with a symlink cannot move this root.
    temporary_root = None if temporary_root is None else temporary_root.resolve()
    if temporary_root is not None and not any(
        temporary_root != root.expanduser().resolve()
        and temporary_root.is_relative_to(root.expanduser().resolve())
        for root in write_roots
    ):
        raise ValueError("Temporary cleanup root must be a strict child of an approved write root")
    def hook(event: str, args: tuple) -> None:
        if event in FORBIDDEN_AUDIT_EVENTS or event.startswith("subprocess."):
            if not _temporary_operation_allowed(event, args, temporary_root):
                raise PermissionError(f"GPU MCP blocked runtime event: {event}")
        if event == "import" and args:
            module_name = str(args[0])
            issue = _runtime_import_rejection_issue(module_name)
            if issue:
                raise PermissionError(f"GPU MCP blocked runtime import: {module_name}")
        if event == "open" and len(args) >= 3:
            path, mode, flags = args[0], args[1], args[2]
            if _open_is_write(mode, flags) and not _write_path_allowed(path, write_roots):
                raise PermissionError(f"GPU MCP blocked write outside approved roots: {path}")
        if event in {"os.open", "os.mkdir"} and len(args) >= 4:
            dir_fd = args[3]
            if dir_fd not in {None, -1}:
                raise PermissionError(f"GPU MCP blocked dir_fd filesystem operation: {event}")
        if event == "sqlite3.connect" and args:
            path = args[0]
            if path != ":memory:" and not path_under_roots(path, write_roots):
                raise PermissionError(f"GPU MCP blocked sqlite database outside approved roots: {path}")
        if event == "os.mkdir" and args:
            path = args[0]
            if not path_under_roots(path, write_roots):
                raise PermissionError(f"GPU MCP blocked directory creation outside approved roots: {path}")

    return hook


def install_preopen_write_guards(write_roots: list[Path]) -> None:
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def guarded_builtin_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if _open_is_write(mode, None) and not _write_path_allowed(file, write_roots):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_builtin_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_io_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if _open_is_write(mode, None) and not _write_path_allowed(file, write_roots):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_io_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if _open_is_write(None, flags) and dir_fd is not None:
            raise PermissionError(f"GPU MCP blocked dir_fd write: {path}")
        if _open_is_write(None, flags) and not _write_path_allowed(path, write_roots):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {path}")
        if dir_fd is None:
            return original_os_open(path, flags, mode)
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    builtins.open = guarded_builtin_open
    io.open = guarded_io_open
    os.open = guarded_os_open


def scan_python_script_safety(script_path: str, *, repo_root: Path, script_roots: list[Path]) -> list[str]:
    script = validate_python_script_path(script_path, repo_root=repo_root, script_roots=script_roots)
    try:
        source = read_script_no_follow(script, script_roots=script_roots)
    except UnicodeDecodeError as e:
        return [f"could not decode script as text: {e}"]
    return scan_python_source_safety(script, source)


def run_job(
    job: str,
    job_args: list[str],
    *,
    repo_root: Path,
    script_roots: list[Path],
    write_roots: list[Path],
    captured_source: str | None = None,
) -> int:
    script = validate_python_script_path(job, repo_root=repo_root, script_roots=script_roots)
    try:
        source = (read_script_no_follow(script, script_roots=script_roots)
                  if captured_source is None else captured_source)
    except UnicodeDecodeError as e:
        print(_script_rejection_message(script, [f"could not decode script as text: {e}"]), file=sys.stderr)
        return 126
    except OSError as e:
        print(f"ERROR: failed to open GPU script without following symlinks: {e}", file=sys.stderr)
        return 126
    issues = scan_python_source_safety(script, source)
    if issues:
        print(_script_rejection_message(script, issues), file=sys.stderr)
        return 126
    temporary_root, temporary_owner, temporary_handles = _prepare_temporary_root(write_roots)
    completed = [False]
    if temporary_root is not None:
        identities = {name: os.fstat(fd) for name, fd in temporary_handles.items()}
        temporary_owner_fd = temporary_handles['owner']
        owner_identity = identities['owner']
        def cleanup_temporary_root():
            try:
                for name, fd in temporary_handles.items():
                    actual, expected = os.fstat(fd), identities[name]
                    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                        return
                current = os.fstat(temporary_owner_fd)
                if (current.st_dev, current.st_ino) != (owner_identity.st_dev, owner_identity.st_ino):
                    return  # A library closed and reused this descriptor.
                if completed[0]:
                    _remove_temporary_directory(temporary_handles['parent'], temporary_handles['directory'], temporary_root.name)
                else:
                    payload = json.dumps(dict(temporary_owner, finished_at=time.time())).encode()
                    if current.st_size <= 4096:
                        os.lseek(temporary_owner_fd, 0, os.SEEK_SET)
                        # Padding avoids truncation, which remains forbidden by
                        # the guard, and replaces any trailing metadata bytes.
                        os.write(temporary_owner_fd, payload.ljust(current.st_size, b' '))
            except OSError:
                pass  # Process identity still protects retained scratch.
            finally:
                for name, fd in temporary_handles.items():
                    try:
                        current = os.fstat(fd)
                        expected = identities[name]
                        if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
                            os.close(fd)
                    except OSError:
                        pass
        # Register before user libraries, allowing their shutdown handlers to
        # finish before successful-job scratch is removed. Retain failed-job
        # scratch for seven days; the job's other outputs are never removed.
        atexit.register(cleanup_temporary_root)
    sys.addaudithook(make_safe_run_audit_hook(write_roots, temporary_root=temporary_root))
    install_preopen_write_guards(write_roots)
    sys.argv = [str(script)] + [str(arg) for arg in job_args]
    sys.path.append(str(script.parent))
    try:
        exec(
            compile(source, str(script), "exec"),
            {
                "__name__": "__main__",
                "__file__": str(script),
                "__package__": None,
                "__cached__": None,
            },
        )
        completed[0] = True
    except SystemExit as exc:
        completed[0] = exc.code in (None, 0)
        raise
    finally:
        if not completed[0] and temporary_root is not None:
            print(f"GPU MCP retained temporary files: {temporary_root}", file=sys.stderr)
        try:
            sys.path.remove(str(script.parent))
        except ValueError:
            pass
    return 0
