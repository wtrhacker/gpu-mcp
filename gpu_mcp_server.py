#!/usr/bin/env python3
"""
MCP server for GPU cluster inspection and constrained Python execution.

Communicates via stdio and uses SSH/Fabric to inspect GPU availability or run
approved Python entrypoints on remote GPU hosts.

Usage (standalone test):
    python gpu_mcp_server.py

Register in Codex or another MCP-aware client as a stdio MCP server.
"""

import sys, os, json, time, subprocess, re, shlex, ast, runpy, builtins, io, hashlib, signal as signal_lib
from pathlib import Path
from typing import Optional

from gpu_mcp_config import ConfigError, GpuMcpPolicy, load_policy

# ── Constants ────────────────────────────────────────────────────────────────


def _pop_config_arg(argv: list[str]) -> str:
    """Remove server-level --config before FastMCP sees argv."""
    if "--config" not in argv:
        return os.environ.get("GPU_MCP_CONFIG", "").strip()
    index = argv.index("--config")
    try:
        value = argv[index + 1]
    except IndexError:
        print("ERROR: --config requires an absolute gpu-mcp.toml path", file=sys.stderr)
        raise SystemExit(2)
    del argv[index : index + 2]
    return value


GPU_MCP_CONFIG_PATH = _pop_config_arg(sys.argv)
CONFIG_POLICY: GpuMcpPolicy | None = None
if GPU_MCP_CONFIG_PATH:
    try:
        CONFIG_POLICY = load_policy(Path(GPU_MCP_CONFIG_PATH).expanduser())
    except ConfigError as exc:
        print(f"ERROR: invalid GPU MCP config: {exc}", file=sys.stderr)
        raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parent
PYTHON = os.environ.get("GPU_MCP_PYTHON", "").strip() or sys.executable
_GPU_MCP_USER_ENV = os.environ.get("GPU_MCP_USER", "").strip()
DEFAULT_GPU_MCP_USER = (
    os.environ.get("USER", "").strip()
    or os.environ.get("LOGNAME", "").strip()
    or Path.home().name
)
GPU_MCP_USER = _GPU_MCP_USER_ENV or DEFAULT_GPU_MCP_USER
GPU_MCP_SSH_KEY = os.environ.get("GPU_MCP_SSH_KEY", "").strip()
GPU_MCP_ALLOW_SSH_FALLBACK = os.environ.get("GPU_MCP_ALLOW_SSH_FALLBACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEFAULT_GPU_MCP_SSH_KEY = Path.home() / ".ssh" / "gpu_mcp_key"


def _env_path_list(name: str) -> list[Path]:
    """Parse a path-list environment variable into Path objects."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]


GPU_MCP_WRITE_ROOTS_RAW = os.environ.get("GPU_MCP_WRITE_ROOTS", "").strip()

APPROVED_SCRIPT_ROOTS = [REPO_ROOT]
APPROVED_OUTPUT_ROOTS = [REPO_ROOT / ".gpu_mcp_logs", Path("/tmp/gpu_mcp_logs")]
APPROVED_WRITE_ROOTS = (
    [REPO_ROOT]
    + APPROVED_OUTPUT_ROOTS
    + [
        Path("/tmp"),
        Path("/tmp/gpu_mcp_outputs"),
        Path("/tmp/gpu_mcp_matplotlib_cache"),
    ]
    + _env_path_list("GPU_MCP_WRITE_ROOTS")
)

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
    "asyncssh",
    "fabric",
    "ftplib",
    "invoke",
    "paramiko",
    "pty",
    "subprocess",
    "telnetlib",
}
RUNTIME_FORBIDDEN_IMPORT_ROOTS = FORBIDDEN_IMPORT_ROOTS - {"subprocess"}
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
}

FORBIDDEN_AUDIT_EVENTS = {
    "os.chmod",
    "os.chown",
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

NODES = [
    "blob.mit.edu",
    "proteome.mit.edu",
    "zubr.mit.edu",
    "wiz.mit.edu",
    "quill.mit.edu",
    "dau.mit.edu",
    "leavitt.mit.edu",
    "ledenberg.mit.edu",
    "dna.mit.edu",
    "something.mit.edu",
    "emmy.mit.edu",
    "sofia.mit.edu",
    "pavlov.mit.edu",
    "evolution.mit.edu",
    "rubin.mit.edu",
    "kulibin.mit.edu",
    "stevens.mit.edu",
]

if CONFIG_POLICY is not None:
    REPO_ROOT = CONFIG_POLICY.repo_root
    NODES = list(CONFIG_POLICY.nodes)
    APPROVED_SCRIPT_ROOTS = list(CONFIG_POLICY.script_roots)
    APPROVED_WRITE_ROOTS = list(CONFIG_POLICY.write_roots)
    APPROVED_OUTPUT_ROOTS = list(CONFIG_POLICY.output_roots)
    GPU_MCP_WRITE_ROOTS_RAW = os.pathsep.join(str(path) for path in APPROVED_WRITE_ROOTS)
SYNC_TIMEOUT_SEC = CONFIG_POLICY.sync_timeout_sec if CONFIG_POLICY is not None else 300

SSH_CONNECT_TIMEOUT = 8

# ── Helpers ──────────────────────────────────────────────────────────────────

def _conn(host: str, user: str = GPU_MCP_USER):
    """Create a Fabric connection using MCP-owned SSH config when available."""
    from fabric import Connection

    connect_kwargs = {}
    key_path = Path(GPU_MCP_SSH_KEY).expanduser() if GPU_MCP_SSH_KEY else DEFAULT_GPU_MCP_SSH_KEY
    if key_path.exists():
        connect_kwargs = {
            "key_filename": str(key_path),
            "allow_agent": False,
            "look_for_keys": False,
        }
    elif not GPU_MCP_ALLOW_SSH_FALLBACK:
        raise RuntimeError(
            "Dedicated GPU MCP SSH key missing. Create ~/.ssh/gpu_mcp_key, "
            "set GPU_MCP_SSH_KEY, or set GPU_MCP_ALLOW_SSH_FALLBACK=1 explicitly."
        )
    return Connection(host, user=user, connect_kwargs=connect_kwargs, connect_timeout=SSH_CONNECT_TIMEOUT)


def _short_host(host: str) -> str:
    """Normalize a host string to its short hostname."""
    value = host.split("@")[-1].strip().lower()
    return value.split(".", 1)[0]


def _node_user_host(node: str) -> tuple[str, str]:
    """Return SSH user and host for a configured node."""
    if "@" in node:
        listed_user, host = node.split("@", 1)
        return _GPU_MCP_USER_ENV or listed_user, host
    return GPU_MCP_USER, node


def _is_local_host(host: str) -> bool:
    """Return True when a requested host is this MCP server's host."""
    requested = host.split("@")[-1].strip().lower()
    if requested in {"localhost", "127.0.0.1", "::1"}:
        return True
    local = os.uname().nodename.lower()
    local_short = local.split(".", 1)[0]
    return requested in {local, local_short, f"{local_short}.mit.edu"} or _short_host(requested) == local_short


def _allowed_host_names() -> set[str]:
    """Return accepted host aliases for tool inputs."""
    hosts: set[str] = {"localhost", "127.0.0.1", "::1"}
    for node in NODES:
        host = node.split("@")[-1].strip().lower()
        short = host.split(".", 1)[0]
        hosts.add(host)
        hosts.add(short)
    return hosts


def _is_allowed_host(host: str) -> bool:
    """Restrict remote tools to configured cluster hosts plus local aliases."""
    requested = host.split("@")[-1].strip().lower()
    return requested in _allowed_host_names() or _is_local_host(requested)


def _local_shell_run(cmd: str, timeout: int = 15) -> Optional[str]:
    """Run a bounded local shell command for GPU status probes."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
            executable="/bin/bash",
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _ssh_run(host: str, cmd: str, user: str = GPU_MCP_USER, hide: bool = True) -> Optional[str]:
    """Run a command on a remote host. Returns stdout or None on failure."""
    try:
        c = _conn(host, user)
        result = c.run(cmd, hide=hide, timeout=15)
        return result.stdout.strip()
    except Exception as e:
        return None


def _host_run(host: str, cmd: str, user: str = GPU_MCP_USER, timeout: int = 15) -> Optional[str]:
    """Run a status command locally for this host, otherwise through SSH."""
    if _is_local_host(host):
        return _local_shell_run(cmd, timeout=timeout)
    return _ssh_run(host, cmd, user=user)


def _parse_nvsmi_csv(raw: str) -> list[dict]:
    """Parse nvidia-smi CSV output into list of dicts."""
    gpus = []
    for line in raw.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "utilization_pct": int(parts[2].replace("%", "").strip()),
                "memory_used_MiB": int(parts[3].replace("MiB", "").strip()),
                "memory_total_MiB": int(parts[4].replace("MiB", "").strip()) if len(parts) > 4 else None,
            })
    return gpus


def _resolve_under(path: str, roots: list[Path], label: str, must_exist: bool) -> Path:
    """Resolve a path and require it to live under one approved root."""
    p = Path(path).expanduser()
    if must_exist and not p.exists():
        raise ValueError(f"{label} does not exist: {path}")
    resolved = p.resolve(strict=must_exist)
    root_paths = [root.expanduser().resolve() for root in roots]
    if not any(resolved == root or root in resolved.parents for root in root_paths):
        allowed = ", ".join(str(root) for root in root_paths)
        raise ValueError(f"{label} must be under approved roots: {allowed}")
    return resolved


def _validate_python_script_path(script_path: str) -> Path:
    """Validate that a remote GPU job targets an approved existing Python file."""
    script = _resolve_under(
        script_path, APPROVED_SCRIPT_ROOTS, "script_path", must_exist=True
    )
    if script.suffix != ".py":
        raise ValueError("script_path must point to a .py file")
    if not script.is_file():
        raise ValueError("script_path must point to a regular file")
    return script


def _validate_output_path(output_file: Optional[str]) -> Path:
    """Validate output path for remote stdout/stderr redirection."""
    if output_file is None:
        return REPO_ROOT / ".gpu_mcp_logs" / f"gpu_python_job_{int(time.time())}.log"
    output = _resolve_under(
        output_file, APPROVED_OUTPUT_ROOTS, "output_file", must_exist=False
    )
    if output.suffix != ".log":
        raise ValueError("output_file must end in .log")
    return output


def _full_call_name(node: ast.AST, aliases: dict[str, str]) -> Optional[str]:
    """Return a dotted call name, resolving simple import aliases."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _full_call_name(node.value, aliases)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Collect simple import aliases so aliased unsafe calls are still caught."""
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
    """Return the top-level module name for import and call checks."""
    return module_name.split(".", 1)[0]


def _import_rejection_issue(module_name: str) -> Optional[str]:
    """Return an issue if an import grants shell or remote-control ability."""
    root = _module_root(module_name)
    if root in FORBIDDEN_IMPORT_ROOTS:
        return f"forbidden import: {module_name}"
    return None


def _runtime_import_rejection_issue(module_name: str) -> Optional[str]:
    """Runtime import guard; process execution itself is blocked by audit events."""
    root = _module_root(module_name)
    if root in RUNTIME_FORBIDDEN_IMPORT_ROOTS:
        return f"forbidden runtime import: {module_name}"
    return None


def _call_rejection_issue(call_name: Optional[str]) -> Optional[str]:
    """Return an issue if a call is unsafe or too dynamic to vet."""
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


def _literal_string_list(node: ast.AST) -> Optional[list[str]]:
    """Extract a list of literal string tokens from a Python literal node."""
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
    """Detect tokenized recursive removal without relying on a shell string."""
    if not tokens or tokens[0] != "rm":
        return False
    for token in tokens[1:]:
        if token == "--recursive":
            return True
        if token.startswith("-") and "r" in token.lower()[1:]:
            return True
    return False


def _dangerous_command_issue(tokens: list[str]) -> Optional[str]:
    """Return a human-readable issue for forbidden shell command tokens."""
    if not tokens:
        return None
    command = Path(tokens[0]).name
    normalized = [command] + tokens[1:]
    if _is_recursive_remove(normalized):
        return "recursive remove command"
    for token in tokens:
        cleaned = token.strip(" \t\r\n;|&(){}[]<>")
        if not cleaned:
            continue
        candidate = Path(cleaned).name
        if candidate in FORBIDDEN_COMMAND_NAMES:
            return f"forbidden command token: {candidate}"
    return None


def _script_rejection_message(script: Path, issues: list[str]) -> str:
    """Build a strong rejection message for unsafe remote GPU scripts."""
    details = "\n".join(f"- {issue}" for issue in issues)
    return (
        f"REJECTED: unsafe Python GPU script: {script}\n"
        f"{details}\n"
        "This file contains destructive, shell, dynamic-execution, or remote-control "
        "behavior. Running it through GPU MCP is blocked because "
        "repo files are shared across hosts. Remove the offending code and rerun "
        "local review before requesting GPU execution. If you think you cannot do your job "
        "without the blocked behavior, please TERMINATE AND STOP WORKING."
    )


def scan_python_gpu_script_safety(script_path: str) -> list[str]:
    """Inspect a Python GPU script and return safety issues without executing it."""
    script = _validate_python_script_path(script_path)
    try:
        source = script.read_text()
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
                method_name = node.func.attr
                if method_name in FORBIDDEN_DESTRUCTIVE_METHOD_NAMES:
                    issue = f"forbidden destructive method: {method_name}"
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


def _path_under_roots(path: object, roots: list[Path]) -> bool:
    """Return True when a path resolves under at least one approved root."""
    if not isinstance(path, (str, bytes, os.PathLike)):
        return True
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except Exception:
        return False
    root_paths = [root.expanduser().resolve(strict=False) for root in roots]
    return any(resolved == root or root in resolved.parents for root in root_paths)


def _open_is_write(mode: object, flags: object) -> bool:
    """Detect write-capable open calls from audit-hook arguments."""
    mode_text = "" if mode is None else str(mode)
    if any(marker in mode_text for marker in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC))
    return False


def _safe_run_audit_hook(event: str, args: tuple) -> None:
    """Runtime guard for scripts launched through run_python_on_gpu.

    Static AST checks are intentionally backed by this audit hook because Python
    can construct dangerous calls dynamically. The hook blocks process launch,
    destructive filesystem operations, socket connections, and writes outside
    approved write roots.
    """
    if event in FORBIDDEN_AUDIT_EVENTS or event.startswith("subprocess."):
        raise PermissionError(f"GPU MCP blocked runtime event: {event}")
    if event == "import" and args:
        module_name = str(args[0])
        issue = _runtime_import_rejection_issue(module_name)
        if issue:
            raise PermissionError(f"GPU MCP blocked runtime import: {module_name}")
    if event == "open" and len(args) >= 3:
        path, mode, flags = args[0], args[1], args[2]
        if _open_is_write(mode, flags) and not _path_under_roots(path, APPROVED_WRITE_ROOTS):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {path}")
    if event == "sqlite3.connect" and args:
        path = args[0]
        if path != ":memory:" and not _path_under_roots(path, APPROVED_WRITE_ROOTS):
            raise PermissionError(f"GPU MCP blocked sqlite database outside approved roots: {path}")
    if event == "os.mkdir" and args:
        path = args[0]
        if not _path_under_roots(path, APPROVED_WRITE_ROOTS):
            raise PermissionError(
                f"GPU MCP blocked directory creation outside approved roots: {path}"
            )


def _install_preopen_write_guards() -> None:
    """Patch Python open entrypoints so write checks happen before OS open."""
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def guarded_builtin_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if _open_is_write(mode, None) and not _path_under_roots(file, APPROVED_WRITE_ROOTS):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_builtin_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_io_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if _open_is_write(mode, None) and not _path_under_roots(file, APPROVED_WRITE_ROOTS):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {file}")
        return original_io_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if _open_is_write(None, flags) and not _path_under_roots(path, APPROVED_WRITE_ROOTS):
            raise PermissionError(f"GPU MCP blocked write outside approved roots: {path}")
        if dir_fd is None:
            return original_os_open(path, flags, mode)
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    builtins.open = guarded_builtin_open
    io.open = guarded_io_open
    os.open = guarded_os_open


def _run_script_under_guard(argv: list[str]) -> int:
    """Run an approved script with runtime audit guards installed."""
    if not argv:
        print("ERROR: --safe-run requires a script path", file=sys.stderr)
        return 2
    if "--" in argv:
        sep = argv.index("--")
        script_arg = argv[0]
        script_args = argv[sep + 1 :]
    else:
        script_arg = argv[0]
        script_args = argv[1:]
    script = _validate_python_script_path(script_arg)
    issues = scan_python_gpu_script_safety(str(script))
    if issues:
        print(_script_rejection_message(script, issues), file=sys.stderr)
        return 126
    sys.addaudithook(_safe_run_audit_hook)
    _install_preopen_write_guards()
    sys.argv = [str(script)] + [str(arg) for arg in script_args]
    runpy.run_path(str(script), run_name="__main__")
    return 0


def _build_python_gpu_argv(script_path: str, args: Optional[list[str]] = None) -> list[str]:
    """Build argv for an approved script launched through the guarded runner."""
    script = _validate_python_script_path(script_path)
    issues = scan_python_gpu_script_safety(str(script))
    if issues:
        raise ValueError(_script_rejection_message(script, issues))
    return [
        PYTHON,
        str(Path(__file__).resolve()),
        "--safe-run",
        str(script),
        "--",
    ] + [str(arg) for arg in (args or [])]


def _build_python_gpu_command(script_path: str, args: Optional[list[str]] = None) -> str:
    """Build a shell-quoted Python command for an approved script."""
    return shlex.join(_build_python_gpu_argv(script_path, args=args))


def _gpu_job_env(gpu_index: int) -> dict[str, str]:
    """Environment for GPU jobs, including cache paths allowed by the guard."""
    env = {
        "CUDA_VISIBLE_DEVICES": str(gpu_index),
        "MPLCONFIGDIR": "/tmp/gpu_mcp_matplotlib_cache",
        "PYTHONDONTWRITEBYTECODE": "1",
        "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
    }
    if GPU_MCP_WRITE_ROOTS_RAW:
        env["GPU_MCP_WRITE_ROOTS"] = GPU_MCP_WRITE_ROOTS_RAW
    if GPU_MCP_CONFIG_PATH:
        env["GPU_MCP_CONFIG"] = str(Path(GPU_MCP_CONFIG_PATH).expanduser().resolve())
    return env


def _shell_env_prefix(env_values: dict[str, str]) -> str:
    """Format env assignments safely for remote shell launch."""
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env_values.items())


def _remote_repo_command(command: str) -> str:
    """Run a remote command from the shared repo root."""
    return f"cd {shlex.quote(str(REPO_ROOT))} && {command}"


KILL_FINGERPRINT_PREFIX = "gpu-mcp-kill-v1:"
KILL_SIGNALS = {
    "TERM": signal_lib.SIGTERM,
    "KILL": signal_lib.SIGKILL,
}


def _normalize_kill_signal(signal_name: object) -> tuple[str, int]:
    """Return the canonical signal name and number for a supported kill signal."""
    text = "KILL" if signal_name is None else str(signal_name).strip().upper()
    if text.startswith("SIG"):
        text = text[3:]
    if text not in KILL_SIGNALS:
        allowed = ", ".join(sorted(KILL_SIGNALS))
        raise ValueError(f"signal must be one of: {allowed}")
    return text, int(KILL_SIGNALS[text])


def _parse_ps_process_line(raw: str) -> Optional[dict[str, str]]:
    """Parse one ps row from the kill inspection query."""
    text = raw.strip()
    if not text:
        return None
    fields = text.split(None, 9)
    if len(fields) < 10:
        return None
    pid, ppid, pgid, owner = fields[:4]
    start_time = " ".join(fields[4:9])
    command = fields[9]
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "owner": owner,
        "start_time": start_time,
        "command": command,
    }


def _process_gpu_usage(host: str, pid: int) -> tuple[Optional[str], Optional[str]]:
    """Return GPU index and memory for a PID when nvidia-smi reports it."""
    proc_query = (
        "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_gpu_memory "
        "--format=csv,noheader,nounits"
    )
    uuid_query = "nvidia-smi --query-gpu=index,uuid --format=csv,noheader"
    proc_raw = _host_run(host, proc_query)
    uuid_raw = _host_run(host, uuid_query)
    if proc_raw is None or uuid_raw is None:
        return None, None

    uuid_to_idx = {}
    for row in uuid_raw.strip().splitlines():
        parts = [part.strip() for part in row.split(",")]
        if len(parts) >= 2:
            uuid_to_idx[parts[1]] = parts[0]

    target = str(pid)
    for row in proc_raw.strip().splitlines():
        parts = [part.strip() for part in row.split(",")]
        if len(parts) < 3 or parts[0] != target:
            continue
        return uuid_to_idx.get(parts[1]), parts[2]
    return None, None


def _compact_command_preview(command: str, max_chars: int = 220) -> str:
    """Return a short single-line command preview for MCP output."""
    preview = " ".join(command.split())
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 3] + "..."


def _kill_fingerprint_payload(host: str, process_info: dict[str, str]) -> dict[str, str]:
    """Fields that must remain stable between inspect and signal calls."""
    return {
        "host": _short_host(host),
        "pid": process_info["pid"],
        "owner": process_info["owner"],
        "ppid": process_info["ppid"],
        "pgid": process_info["pgid"],
        "start_time": process_info["start_time"],
        "cmd_hash": process_info["cmd_hash"],
    }


def _kill_fingerprint(host: str, process_info: dict[str, str]) -> str:
    """Build a stateless fingerprint for a process identity check."""
    payload = _kill_fingerprint_payload(host, process_info)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return KILL_FINGERPRINT_PREFIX + hashlib.sha256(encoded).hexdigest()


def _inspect_kill_target(host: str, pid: int) -> Optional[dict[str, str]]:
    """Inspect a single process for owner/fingerprint-based cancellation."""
    ps_cmd = f"ps -p {pid} -o pid= -o ppid= -o pgid= -o user= -o lstart= -o args="
    ps_raw = _host_run(host, ps_cmd)
    if ps_raw is None:
        return None
    info = _parse_ps_process_line(ps_raw)
    if info is None:
        return None

    command = info["command"]
    info["cmd_hash"] = hashlib.sha256(command.encode()).hexdigest()
    info["cmd_preview"] = _compact_command_preview(command)
    gpu_index, gpu_memory = _process_gpu_usage(host, pid)
    info["gpu_index"] = "" if gpu_index is None else str(gpu_index)
    info["gpu_memory_mib"] = "" if gpu_memory is None else str(gpu_memory)
    info["fingerprint"] = _kill_fingerprint(host, info)
    return info


def _kill_process_response(
    *,
    status: str,
    host: str,
    pid: int,
    process_info: Optional[dict[str, str]] = None,
    killable: bool = False,
    reason: str = "",
    signal_sent: Optional[str] = None,
) -> str:
    """Return compact JSON for kill_gpu_process."""
    response = {
        "status": status,
        "host": host,
        "pid": str(pid),
        "killable": killable,
        "reason": reason,
        "signal_sent": signal_sent,
    }
    if process_info:
        response.update({
            "owner": process_info["owner"],
            "ppid": process_info["ppid"],
            "pgid": process_info["pgid"],
            "start_time": process_info["start_time"],
            "cmd_hash": process_info["cmd_hash"],
            "cmd_preview": process_info["cmd_preview"],
            "gpu_index": process_info["gpu_index"],
            "gpu_memory_mib": process_info["gpu_memory_mib"],
            "fingerprint": process_info["fingerprint"],
        })
    return json.dumps(response, sort_keys=True)


# ── MCP Server ───────────────────────────────────────────────────────────────

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--safe-run":
    raise SystemExit(_run_script_under_guard(sys.argv[2:]))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gpu-cluster", instructions=(
    "GPU cluster management server. Provides tools to check GPU availability "
    "across the MIT compute cluster, run approved Python files on specific "
    "GPUs, identify which processes own which GPUs, and signal only GPU_MCP_USER "
    "processes after fingerprint confirmation. kill_gpu_process is not a "
    "general cleanup or scheduling tool: use it only for a specific PID that "
    "the caller intends to stop. First inspect the target, read the owner, GPU, "
    "start time, process group, and command preview, then pass the returned "
    "fingerprint only if it is still clearly the intended process."
))


# Report known inference-capable GPU nodes.
# Most are RTX 4090 hosts; sofia has 4x RTX 3080 and is useful for lighter jobs.
RTX4090_HOSTS = {
    "blob",
    "wiz",
    "leavitt",
    "ledenberg",
    "dna",
    "something",
    "emmy",
    "pavlov",
    "evolution",
    "stevens",
    "kulibin",
    "sofia",
}


@mcp.tool()
def check_gpus(
    samples: int = 2,
    threshold: int = 10,
):
    """Check GPU availability across the cluster.

    Uses check_gpus.bash which SSHes to all nodes, takes multiple
    nvidia-smi samples, averages utilization, and reports AVAILABLE/BUSY.
    Only shows RTX 4090 nodes (others lack memory for inference).

    Args:
        samples: Number of utilization samples to average (default 2).
        threshold: GPU utilization % at or below which a GPU is marked AVAILABLE (default 10).

    Returns:
        Formatted report of GPU status across the cluster.
    """
    samples = max(1, int(samples))
    threshold = int(threshold)
    query = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits"
    )
    lines = [
        f"==> Starting GPU check: {samples} samples, threshold <= {threshold}%"
    ]
    remote_successes = 0

    for node in NODES:
        user, host = _node_user_host(node)
        short_host = host.split(".", 1)[0]
        if short_host not in RTX4090_HOSTS:
            continue
        local_host = _is_local_host(host)
        route = "local" if local_host else "ssh"

        sample_sets: list[list[dict]] = []
        failed = False
        for _ in range(samples):
            raw = _host_run(host, query, user=user)
            if raw is None:
                failed = True
                break
            sample_sets.append(_parse_nvsmi_csv(raw))
            if samples > 1:
                time.sleep(1)

        lines.append(f"[{short_host} {route}]")
        if failed or not sample_sets:
            lines.append("  ssh/nvidia-smi failed")
            continue
        if not local_host:
            remote_successes += 1

        ngpus = max(len(sample) for sample in sample_sets)
        for gpu_index in range(ngpus):
            seen = [sample[gpu_index] for sample in sample_sets if gpu_index < len(sample)]
            if not seen:
                continue
            avg_util = sum(g["utilization_pct"] for g in seen) / len(seen)
            last = seen[-1]
            mem_free = (
                int(last["memory_total_MiB"] or 0) - int(last["memory_used_MiB"])
                if last["memory_total_MiB"] is not None
                else None
            )
            status = "AVAILABLE" if avg_util <= threshold and (mem_free is None or mem_free >= 16000) else "BUSY"
            mem_text = (
                f"{last['memory_used_MiB']}/{last['memory_total_MiB']} MiB"
                if last["memory_total_MiB"] is not None
                else f"{last['memory_used_MiB']} MiB"
            )
            lines.append(
                f"  GPU {last['index']} | {last['name']} | util_avg={avg_util:.1f}% | "
                f"mem={mem_text} | {status}"
            )

    if remote_successes == 0:
        lines.append(
            "WARNING: no non-local SSH GPU host succeeded; remote MCP SSH "
            "routing is not installed from this control host."
        )

    return "\n".join(lines) if len(lines) > 1 else "(no RTX 4090 nodes available)"


@mcp.tool()
def check_gpu_processes(
    hosts: Optional[list[str]] = None,
    user_filter: Optional[str] = None,
):
    """Identify which processes are using GPUs and who owns them.

    This is the reliable way to check if YOUR code is running on a GPU,
    rather than just checking utilization percentage.

    Args:
        hosts: List of hostnames to check. Defaults to all nodes.
        user_filter: Only show processes owned by this user (default: GPU_MCP_USER).
                     Set to empty string to show all users.

    Returns:
        Per-host report of GPU-owning processes with PID, user, command, and memory usage.
    """
    if hosts is None:
        hosts = [n.split("@")[-1] for n in NODES]
    if user_filter is None:
        user_filter = GPU_MCP_USER

    # nvidia-smi query for compute processes
    proc_query = (
        "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_gpu_memory,process_name "
        "--format=csv,noheader,nounits"
    )
    # Also get GPU index↔UUID mapping
    uuid_query = "nvidia-smi --query-gpu=index,uuid --format=csv,noheader"

    lines = []
    for host in hosts:
        if not _is_allowed_host(host):
            lines.append(f"[{host}] rejected: host is not in GPU MCP NODES")
            continue
        proc_raw = _host_run(host, proc_query)
        uuid_raw = _host_run(host, uuid_query)

        if proc_raw is None or uuid_raw is None:
            lines.append(f"[{host}] SSH/nvidia-smi failed")
            continue

        # Build UUID → index map
        uuid_to_idx = {}
        for row in uuid_raw.strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 2:
                uuid_to_idx[parts[1]] = int(parts[0])

        if not proc_raw.strip():
            lines.append(f"[{host}] no GPU compute processes running")
            continue

        lines.append(f"[{host}]")
        for row in proc_raw.strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 4:
                continue
            pid, gpu_uuid, mem_mib, proc_name = parts[0], parts[1], parts[2], parts[3]
            gpu_idx = uuid_to_idx.get(gpu_uuid, "?")

            # Get the owning user and full command via ps
            ps_raw = _host_run(host, f"ps -p {pid} -o user=,args= 2>/dev/null")
            if ps_raw:
                ps_parts = ps_raw.split(None, 1)
                owner = ps_parts[0] if ps_parts else "?"
                cmd = ps_parts[1] if len(ps_parts) > 1 else proc_name
            else:
                owner = "?"
                cmd = proc_name

            if user_filter and owner != user_filter:
                continue

            lines.append(
                f"  GPU {gpu_idx} | PID {pid} | user={owner} | mem={mem_mib} MiB | {cmd}"
            )

    return "\n".join(lines) if lines else "No matching GPU processes found."


@mcp.tool()
def kill_gpu_process(
    host: str,
    pid: int,
    fingerprint: Optional[str] = None,
    signal: str = "KILL",
):
    """Inspect or signal one intended GPU process on a configured host.

    This is not a broad process-management tool. Do not use it for general
    cleanup, GPU-index cleanup, or guessed PIDs. Use it only when the caller has
    a specific host/PID target and the inspected owner, GPU, start time, process
    group, and command preview match the process they intend to stop. If there
    is any uncertainty, inspect the process again or use check_gpu_processes
    before signaling.

    This tool is intentionally stateless. Call it once without a fingerprint to
    inspect a single host/PID and receive a fingerprint. Call it again with that
    fingerprint to signal the process, after the server rechecks the owner,
    start time, process group, and command hash. It refuses processes not owned
    by GPU_MCP_USER.

    Args:
        host: Configured host or host alias.
        pid: Process ID on that host.
        fingerprint: Fingerprint from a previous inspect call. If omitted,
                     this call only inspects and does not send a signal.
        signal: TERM or KILL. Defaults to KILL.

    Returns:
        Compact JSON describing the inspected target and whether a signal was sent.
    """
    try:
        pid_int = int(pid)
    except Exception:
        return json.dumps({
            "status": "refused",
            "host": host,
            "pid": str(pid),
            "killable": False,
            "reason": "pid must be an integer",
            "signal_sent": None,
        }, sort_keys=True)

    if pid_int <= 0:
        return _kill_process_response(
            status="refused",
            host=host,
            pid=pid_int,
            reason="pid must be positive",
        )
    if not _is_allowed_host(host):
        return _kill_process_response(
            status="refused",
            host=host,
            pid=pid_int,
            reason="host must be one of the configured GPU MCP NODES",
        )

    try:
        signal_name, signal_number = _normalize_kill_signal(signal)
    except ValueError as e:
        return _kill_process_response(
            status="refused",
            host=host,
            pid=pid_int,
            reason=str(e),
        )

    process_info = _inspect_kill_target(host, pid_int)
    if process_info is None:
        return _kill_process_response(
            status="refused",
            host=host,
            pid=pid_int,
            reason="process not found or could not be inspected",
        )

    if process_info["owner"] != GPU_MCP_USER:
        return _kill_process_response(
            status="inspect" if fingerprint is None else "refused",
            host=host,
            pid=pid_int,
            process_info=process_info,
            killable=False,
            reason=(
                f"owner {process_info['owner']} does not match "
                f"GPU_MCP_USER {GPU_MCP_USER}"
            ),
        )

    if fingerprint is None:
        return _kill_process_response(
            status="inspect",
            host=host,
            pid=pid_int,
            process_info=process_info,
            killable=True,
        )

    if fingerprint != process_info["fingerprint"]:
        return _kill_process_response(
            status="refused",
            host=host,
            pid=pid_int,
            process_info=process_info,
            killable=True,
            reason="fingerprint mismatch; inspect the process again before signaling",
        )

    kill_result = _host_run(host, f"kill -{signal_number} {pid_int}")
    if kill_result is None:
        return _kill_process_response(
            status="error",
            host=host,
            pid=pid_int,
            process_info=process_info,
            killable=True,
            reason="signal command failed",
        )

    return _kill_process_response(
        status="signaled",
        host=host,
        pid=pid_int,
        process_info=process_info,
        killable=True,
        signal_sent=signal_name,
    )


@mcp.tool()
def run_python_on_gpu(
    host: str,
    gpu_index: int,
    script_path: str,
    args: Optional[list[str]] = None,
    async_mode: bool = False,
    output_file: Optional[str] = None,
):
    """Run an approved Python file on a specific GPU on a specific host.

    Args:
        host: Hostname (e.g. "blob.mit.edu").
        gpu_index: GPU device index to use (sets CUDA_VISIBLE_DEVICES).
        script_path: Existing .py file under an approved script root.
        args: Positional CLI args passed to the Python script.
        async_mode: If True, run in background and return immediately with PID.
        output_file: Approved path for stdout/stderr capture (used with async_mode).

    Returns:
        Command output (sync) or PID info (async).
    """
    if not isinstance(gpu_index, int) or gpu_index < 0:
        return "ERROR: gpu_index must be a non-negative integer"
    if not _is_allowed_host(host):
        return "ERROR: host must be one of the configured GPU MCP NODES"

    try:
        argv = _build_python_gpu_argv(script_path, args=args)
        command = shlex.join(argv)
        out_path = _validate_output_path(output_file)
    except ValueError as e:
        message = str(e)
        if message.startswith("REJECTED:"):
            return message
        return f"ERROR: {message}"

    job_env = _gpu_job_env(gpu_index)
    env_prefix = _shell_env_prefix(job_env)

    if async_mode:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"ERROR: Failed to create output directory {out_path.parent}: {e}"
        if _is_local_host(host):
            try:
                env = os.environ.copy()
                env.update(job_env)
                out_handle = open(out_path, "w")
                proc = subprocess.Popen(
                    argv,
                    stdout=out_handle,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=str(REPO_ROOT),
                    start_new_session=True,
                )
                out_handle.close()
            except Exception as e:
                return f"ERROR: Failed to launch local GPU job on {host}: {e}"
            return json.dumps({
                "status": "launched",
                "host": host,
                "gpu_index": gpu_index,
                "pid": str(proc.pid),
                "output_file": str(out_path),
                "launch": "local",
            })
        launch_cmd = (
            f"(nohup env {env_prefix} {command} > {shlex.quote(str(out_path))} "
            "2>&1 < /dev/null & echo $!)"
        )
        bg_cmd = (
            f"mkdir -p {shlex.quote(str(out_path.parent))} && "
            f"{_remote_repo_command(launch_cmd)}"
        )
        pid_str = _ssh_run(host, bg_cmd)
        if pid_str is None:
            return f"ERROR: Failed to SSH to {host}"
        return json.dumps({
            "status": "launched",
            "host": host,
            "gpu_index": gpu_index,
            "pid": pid_str.strip(),
            "output_file": str(out_path),
            "launch": "ssh",
        })
    else:
        if _is_local_host(host):
            try:
                env = os.environ.copy()
                env.update(job_env)
                result = subprocess.run(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=SYNC_TIMEOUT_SEC,
                    env=env,
                    cwd=str(REPO_ROOT),
                )
            except Exception as e:
                return f"ERROR: Failed to run local GPU job on {host}: {e}"
            if result.returncode != 0:
                return (
                    f"ERROR: local GPU job exited with status {result.returncode}\n"
                    f"{result.stdout}{result.stderr}"
                )
            return result.stdout
        try:
            c = _conn(host)
            result = c.run(
                _remote_repo_command(f"env {env_prefix} {command}"),
                hide=True,
                timeout=SYNC_TIMEOUT_SEC,
            )
            return result.stdout
        except Exception as e:
            return f"ERROR: {e}"


@mcp.tool()
def cluster_info():
    """Get a quick overview of the entire cluster: which nodes are reachable, GPU counts, load.

    Returns:
        Summary table of all cluster nodes.
    """
    query = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits 2>/dev/null; echo '|LOAD|'; uptime"
    )
    lines = ["HOST                | STATUS    | GPUs | GPU_UTIL_AVG | LOAD_AVG"]
    lines.append("-" * 72)

    for node in NODES:
        user, host = _node_user_host(node)
        raw = _host_run(host, query, user=user)
        if raw is None:
            lines.append(f"{host:20s} | OFFLINE   |    - |            - | -")
            continue

        parts = raw.split("|LOAD|")
        gpu_raw = parts[0].strip() if parts else ""
        load_raw = parts[1].strip() if len(parts) > 1 else ""

        gpus = _parse_nvsmi_csv(gpu_raw) if gpu_raw else []
        avg_util = sum(g["utilization_pct"] for g in gpus) // max(len(gpus), 1) if gpus else 0

        # Parse load average from uptime
        load_match = re.search(r"load average:\s*([\d.]+)", load_raw)
        load_avg = load_match.group(1) if load_match else "?"

        lines.append(
            f"{host:20s} | ONLINE    | {len(gpus):4d} | {avg_util:10d}% | {load_avg}"
        )

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
