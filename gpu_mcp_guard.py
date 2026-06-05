from __future__ import annotations

"""Shared stdlib guard logic for GPU MCP Python job execution."""

import ast
import builtins
import io
import os
import shlex
import sys
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


def make_safe_run_audit_hook(write_roots: list[Path]):
    def hook(event: str, args: tuple) -> None:
        if event in FORBIDDEN_AUDIT_EVENTS or event.startswith("subprocess."):
            raise PermissionError(f"GPU MCP blocked runtime event: {event}")
        if event == "import" and args:
            module_name = str(args[0])
            issue = _runtime_import_rejection_issue(module_name)
            if issue:
                raise PermissionError(f"GPU MCP blocked runtime import: {module_name}")
        if event == "open" and len(args) >= 3:
            path, mode, flags = args[0], args[1], args[2]
            if _open_is_write(mode, flags) and not path_under_roots(path, write_roots):
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
        if _open_is_write(mode, None) and not path_under_roots(file, write_roots):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_builtin_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_io_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if _open_is_write(mode, None) and not path_under_roots(file, write_roots):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_io_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if _open_is_write(None, flags) and dir_fd is not None:
            raise PermissionError(f"GPU MCP blocked dir_fd write: {path}")
        if _open_is_write(None, flags) and not path_under_roots(path, write_roots):
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
) -> int:
    script = validate_python_script_path(job, repo_root=repo_root, script_roots=script_roots)
    try:
        source = read_script_no_follow(script, script_roots=script_roots)
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
    sys.addaudithook(make_safe_run_audit_hook(write_roots))
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
    finally:
        try:
            sys.path.remove(str(script.parent))
        except ValueError:
            pass
    return 0
