#!/usr/bin/env python3
"""
MCP server for GPU cluster inspection and constrained Python execution.

Communicates via stdio and uses SSH/Fabric to inspect GPU availability or run
approved Python entrypoints on remote GPU hosts.

Usage:
    python gpu_mcp_server.py --config /absolute/path/to/gpu-mcp.toml

Register in Codex or another MCP-aware client as a stdio MCP server.
"""

import sys, os, json, time, subprocess, re, shlex, hashlib, signal as signal_lib, secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import gpu_mcp_guard
from gpu_mcp_config import ConfigError, GpuMcpPolicy, load_policy
from gpu_mcp_policy_approval import (
    PolicyApprovalError,
    approve_policy,
    diff_policy_summary,
    policy_file_hash,
    verify_policy_approved,
)

# ── Constants ────────────────────────────────────────────────────────────────


def _pop_config_arg(argv: list[str]) -> str:
    """Remove server-level --config before FastMCP sees argv."""
    if "--config" not in argv:
        print("ERROR: --config requires an absolute gpu-mcp.toml path", file=sys.stderr)
        raise SystemExit(2)
    index = argv.index("--config")
    try:
        value = argv[index + 1]
    except IndexError:
        print("ERROR: --config requires an absolute gpu-mcp.toml path", file=sys.stderr)
        raise SystemExit(2)
    del argv[index : index + 2]
    if not Path(value).expanduser().is_absolute():
        print("ERROR: --config requires an absolute gpu-mcp.toml path", file=sys.stderr)
        raise SystemExit(2)
    return value


GPU_MCP_CONFIG_PATH = _pop_config_arg(sys.argv)
GPU_MCP_TEST_DISABLE_POLICY_APPROVAL = (
    os.environ.get("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", "").strip().lower()
    in {"1", "true", "yes"}
    and "PYTEST_CURRENT_TEST" in os.environ
)
CONFIG_POLICY: GpuMcpPolicy | None = None
try:
    CONFIG_POLICY = load_policy(Path(GPU_MCP_CONFIG_PATH).expanduser())
    if not GPU_MCP_TEST_DISABLE_POLICY_APPROVAL:
        verify_policy_approved(CONFIG_POLICY.config_path)
except ConfigError as exc:
    print(f"ERROR: invalid GPU MCP config: {exc}", file=sys.stderr)
    raise SystemExit(2)
except PolicyApprovalError as exc:
    print(f"ERROR: unapproved GPU MCP policy: {exc}", file=sys.stderr)
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

def _apply_policy(policy: GpuMcpPolicy) -> None:
    global CONFIG_POLICY
    global REPO_ROOT, NODES
    global APPROVED_SCRIPT_ROOTS, APPROVED_WRITE_ROOTS, APPROVED_OUTPUT_ROOTS
    global GPU_MCP_WRITE_ROOTS_RAW, SYNC_TIMEOUT_SEC

    CONFIG_POLICY = policy
    REPO_ROOT = policy.repo_root
    NODES = list(policy.nodes)
    APPROVED_SCRIPT_ROOTS = list(policy.script_roots)
    APPROVED_WRITE_ROOTS = list(policy.write_roots)
    APPROVED_OUTPUT_ROOTS = list(policy.output_roots)
    GPU_MCP_WRITE_ROOTS_RAW = os.pathsep.join(str(path) for path in APPROVED_WRITE_ROOTS)
    SYNC_TIMEOUT_SEC = policy.sync_timeout_sec


_apply_policy(CONFIG_POLICY)
ACTIVE_POLICY_HASH = policy_file_hash(CONFIG_POLICY.config_path)
ACTIVE_POLICY_FILE_STAT = CONFIG_POLICY.config_path.stat()
_STALE_POLICY_HASH_CACHE = {
    "signature": (
        ACTIVE_POLICY_FILE_STAT.st_mtime_ns,
        ACTIVE_POLICY_FILE_STAT.st_size,
    ),
    "hash": ACTIVE_POLICY_HASH,
}
PENDING_POLICY_RELOADS: dict[str, dict[str, object]] = {}
POLICY_RELOAD_TOKEN_TTL_SEC = 60 * 60
POLICY_RELOAD_MAX_PENDING = 64

SSH_CONNECT_TIMEOUT = 8
HOST_RUN_ERRORS: dict[tuple[str, str], str] = {}

POLICY_RELOAD_AGENT_INSTRUCTIONS = [
    "Show this raw preview output, including diff_summary and hashes, to the human.",
    "Do not summarize it as the only evidence.",
    "Call reload_policy only after explicit human approval.",
    "If you edited gpu-mcp.toml yourself, remind the human to inspect the file before approving reload.",
]


def _cleanup_policy_reload_tokens(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    expired = [
        token
        for token, pending in PENDING_POLICY_RELOADS.items()
        if now - float(pending.get("created_at", 0.0)) > POLICY_RELOAD_TOKEN_TTL_SEC
    ]
    for token in expired:
        PENDING_POLICY_RELOADS.pop(token, None)

    while len(PENDING_POLICY_RELOADS) > POLICY_RELOAD_MAX_PENDING:
        oldest = min(
            PENDING_POLICY_RELOADS,
            key=lambda token: float(PENDING_POLICY_RELOADS[token].get("created_at", 0.0)),
        )
        PENDING_POLICY_RELOADS.pop(oldest, None)


def _pop_pending_policy_reload(token: str) -> tuple[dict[str, object] | None, str | None]:
    pending = PENDING_POLICY_RELOADS.get(token)
    if pending is None:
        return None, "invalid or already used reload token"
    now = time.monotonic()
    if now - float(pending.get("created_at", 0.0)) > POLICY_RELOAD_TOKEN_TTL_SEC:
        PENDING_POLICY_RELOADS.pop(token, None)
        return None, "expired reload token; preview again"
    return PENDING_POLICY_RELOADS.pop(token), None

STALE_POLICY_REFUSAL = (
    "gpu-mcp.toml has changed but has not been reloaded.\n"
    "Active policy is still the old approved policy.\n"
    "Do not revert the file. Do not edit any policy or Codex config file.\n"
    "Stop immediately and explain to the human what you were trying to do, "
    "what changed, and why GPU MCP refused to continue.\n"
    "If the human intentionally changed the policy, the next step is "
    "preview_policy_reload. Show the safety diff and call reload_policy only "
    "after explicit human approval."
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _conn(host: str, user: str = GPU_MCP_USER):
    """Create a Fabric connection using MCP-owned SSH config when available."""
    from fabric import Connection

    connect_kwargs = {}
    key_path = Path(GPU_MCP_SSH_KEY).expanduser() if GPU_MCP_SSH_KEY else DEFAULT_GPU_MCP_SSH_KEY
    if key_path.is_symlink():
        raise RuntimeError(f"Dedicated GPU MCP SSH key must not be a symlink: {key_path}")
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


def _stale_policy_refusal() -> str | None:
    try:
        stat = CONFIG_POLICY.config_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if _STALE_POLICY_HASH_CACHE.get("signature") == signature:
            current_hash = str(_STALE_POLICY_HASH_CACHE["hash"])
        else:
            current_hash = policy_file_hash(CONFIG_POLICY.config_path)
            _STALE_POLICY_HASH_CACHE["signature"] = signature
            _STALE_POLICY_HASH_CACHE["hash"] = current_hash
    except Exception as exc:
        return f"{STALE_POLICY_REFUSAL}\nCurrent policy file could not be hashed: {exc}"
    if current_hash != ACTIVE_POLICY_HASH:
        return STALE_POLICY_REFUSAL
    return None


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
    return requested in {local, local_short} or _short_host(requested) == local_short


def _allowed_host_names() -> set[str]:
    """Return accepted host aliases for tool inputs."""
    hosts: set[str] = set()
    for node in NODES:
        host = node.split("@")[-1].strip().lower()
        short = host.split(".", 1)[0]
        hosts.add(host)
        hosts.add(short)
    return hosts


def _is_allowed_host(host: str) -> bool:
    """Restrict tools to configured repo policy hosts."""
    requested = host.split("@")[-1].strip().lower()
    return requested in _allowed_host_names()


def _host_run_error(host: str, cmd: str) -> str:
    return HOST_RUN_ERRORS.get((host, cmd), "")


def _record_host_run_error(host: str, cmd: str, message: str) -> None:
    HOST_RUN_ERRORS[(host, cmd)] = message


def _classify_run_exception(exc: BaseException) -> str:
    text = str(exc).strip()
    lowered = text.lower()
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in lowered or "timeout" in lowered:
        return f"timeout: {text}"
    if "auth" in lowered or "permission denied" in lowered or "publickey" in lowered:
        return f"authentication failed: {text}"
    if "refused" in lowered or "could not resolve" in lowered or "name or service" in lowered:
        return f"connection failed: {text}"
    return text or exc.__class__.__name__


def _local_shell_run(cmd: str, timeout: int = 15) -> Optional[str]:
    """Run a bounded local probe command without invoking a shell."""
    HOST_RUN_ERRORS.pop(("local", cmd), None)
    try:
        argv = shlex.split(cmd)
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
    except Exception as exc:
        _record_host_run_error("local", cmd, _classify_run_exception(exc))
        return None
    if result.returncode != 0:
        _record_host_run_error(
            "local",
            cmd,
            f"exit {result.returncode}: {(result.stderr or result.stdout).strip()}",
        )
        return None
    return f"{result.stdout}{result.stderr}".strip()


def _ssh_run(
    host: str,
    cmd: str,
    user: str = GPU_MCP_USER,
    hide: bool = True,
    timeout: int = 15,
) -> Optional[str]:
    """Run a command on a remote host. Returns stdout or None on failure."""
    HOST_RUN_ERRORS.pop((host, cmd), None)
    try:
        c = _conn(host, user)
        result = c.run(cmd, hide=hide, timeout=timeout)
        return result.stdout.strip()
    except Exception as e:
        _record_host_run_error(host, cmd, _classify_run_exception(e))
        return None


def _host_run(host: str, cmd: str, user: str = GPU_MCP_USER, timeout: int = 15) -> Optional[str]:
    """Run a status command locally for this host, otherwise through SSH."""
    if _is_local_host(host):
        return _local_shell_run(cmd, timeout=timeout)
    return _ssh_run(host, cmd, user=user, timeout=timeout)


def _parse_optional_int(value: str) -> Optional[int]:
    text = value.replace("%", "").replace("MiB", "").strip()
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_nvsmi_csv(raw: str) -> list[dict]:
    """Parse nvidia-smi CSV output into list of dicts."""
    gpus = []
    for line in raw.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            gpu_index = _parse_optional_int(parts[0])
            if gpu_index is None:
                continue
            gpus.append({
                "index": gpu_index,
                "name": parts[1],
                "utilization_pct": _parse_optional_int(parts[2]),
                "memory_used_MiB": _parse_optional_int(parts[3]),
                "memory_total_MiB": _parse_optional_int(parts[4]) if len(parts) > 4 else None,
            })
    return gpus


def _resolve_under(path: str, roots: list[Path], label: str, must_exist: bool) -> Path:
    """Resolve a path and require it to live under one approved root."""
    return gpu_mcp_guard.resolve_under(
        path,
        repo_root=REPO_ROOT,
        roots=roots,
        label=label,
        must_exist=must_exist,
    )


def _validate_python_script_path(script_path: str) -> Path:
    """Validate that a remote GPU job targets an approved existing Python file."""
    return gpu_mcp_guard.validate_python_script_path(
        script_path,
        repo_root=REPO_ROOT,
        script_roots=APPROVED_SCRIPT_ROOTS,
    )


def _open_output_no_follow(path: Path):
    """Open an output file without following a final-component symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        fd_path = Path(f"/proc/self/fd/{fd}")
        if fd_path.exists() and not _path_under_roots(fd_path.resolve(), APPROVED_OUTPUT_ROOTS):
            raise PermissionError(f"GPU MCP blocked output outside approved roots: {path}")
        return os.fdopen(fd, "w")
    except Exception:
        os.close(fd)
        raise


def _prepare_output_parent(path: Path) -> None:
    """Require an already-created, non-symlink output parent under output roots."""
    parent = path.parent
    if parent.is_symlink():
        raise ValueError(f"output_file parent must not be a symlink: {parent}")
    if not parent.exists():
        raise ValueError(f"output_file parent directory must already exist: {parent}")
    if not parent.is_dir():
        raise ValueError(f"output_file parent must be a directory: {parent}")
    if not _path_under_roots(parent, APPROVED_OUTPUT_ROOTS):
        raise ValueError(f"output_file parent must be under approved roots: {parent}")


def _validate_output_path(output_file: Optional[str]) -> Path:
    """Validate output path for remote stdout/stderr redirection."""
    if output_file is None:
        return _resolve_under(
            str(APPROVED_OUTPUT_ROOTS[0] / f"gpu_python_job_{int(time.time())}.log"),
            APPROVED_OUTPUT_ROOTS,
            "output_file",
            must_exist=False,
        )
    output = _resolve_under(
        output_file, APPROVED_OUTPUT_ROOTS, "output_file", must_exist=False
    )
    if output.suffix != ".log":
        raise ValueError("output_file must end in .log")
    return output


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runner_bundle_sources() -> dict[str, bytes]:
    base = Path(__file__).resolve().parent
    return {
        "gpu_mcp_safe_runner.py": (base / "gpu_mcp_safe_runner.py").read_bytes(),
        "gpu_mcp_guard.py": (base / "gpu_mcp_guard.py").read_bytes(),
    }


def _safe_runner_source_hash() -> str:
    return hashlib.sha256(_runner_bundle_sources()["gpu_mcp_safe_runner.py"]).hexdigest()


def _ensure_staged_safe_runner() -> Path:
    """Stage the standalone runner into the repo without following symlinks."""
    runner_dir = REPO_ROOT / ".gpu_mcp_runner"
    if runner_dir.exists() and runner_dir.is_symlink():
        raise ValueError(f"safe runner directory must not be a symlink: {runner_dir}")
    runner_dir.mkdir(mode=0o700, exist_ok=True)
    bundle = _runner_bundle_sources()
    file_hashes: dict[str, str] = {}
    for filename, source in bundle.items():
        target = runner_dir / filename
        expected_hash = hashlib.sha256(source).hexdigest()
        file_hashes[filename] = expected_hash
        needs_write = True
        if target.exists() and not target.is_symlink():
            try:
                needs_write = _file_sha256(target) != expected_hash
            except OSError:
                needs_write = True

        if needs_write:
            tmp_path = runner_dir / f".{filename}.{os.getpid()}.tmp"
            tmp_path.write_bytes(source)
            tmp_path.chmod(0o700)
            os.replace(tmp_path, target)
            print(
                f"GPU MCP staged runner bundle file at {target} sha256={expected_hash}",
                file=sys.stderr,
            )

    manifest_path = runner_dir / "runner_manifest.json"
    manifest = {
        "schema_version": 1,
        "runner_path": str(runner_dir / "gpu_mcp_safe_runner.py"),
        "files": {
            filename: {"sha256": file_hash}
            for filename, file_hash in sorted(file_hashes.items())
        },
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    tmp_manifest = runner_dir / f".runner_manifest.{os.getpid()}.tmp"
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_manifest, manifest_path)
    return runner_dir / "gpu_mcp_safe_runner.py"


def _script_rejection_message(script: Path, issues: list[str]) -> str:
    """Build a strong rejection message for unsafe remote GPU scripts."""
    return gpu_mcp_guard._script_rejection_message(script, issues)


def _read_script_no_follow(script: Path) -> str:
    """Read an already validated script without following a swapped symlink."""
    return gpu_mcp_guard.read_script_no_follow(script, script_roots=APPROVED_SCRIPT_ROOTS)


def _scan_python_source_safety(script: Path, source: str) -> list[str]:
    """Inspect Python source and return safety issues without executing it."""
    return gpu_mcp_guard.scan_python_source_safety(script, source)


def scan_python_gpu_script_safety(script_path: str) -> list[str]:
    """Inspect a Python GPU script and return safety issues without executing it."""
    return gpu_mcp_guard.scan_python_script_safety(
        script_path,
        repo_root=REPO_ROOT,
        script_roots=APPROVED_SCRIPT_ROOTS,
    )


def _path_under_roots(path: object, roots: list[Path]) -> bool:
    """Return True when a path resolves under at least one approved root."""
    return gpu_mcp_guard.path_under_roots(path, roots)


def _open_is_write(mode: object, flags: object) -> bool:
    """Detect write-capable open calls from audit-hook arguments."""
    return gpu_mcp_guard._open_is_write(mode, flags)


def _safe_run_audit_hook(event: str, args: tuple) -> None:
    """Runtime guard for scripts launched through run_python_on_gpu.

    Static AST checks are intentionally backed by this audit hook because Python
    can construct dangerous calls dynamically. The hook blocks process launch,
    destructive filesystem operations, socket connections, and writes outside
    approved write roots.
    """
    return gpu_mcp_guard.make_safe_run_audit_hook(APPROVED_WRITE_ROOTS)(event, args)


def _install_preopen_write_guards() -> None:
    """Patch Python open entrypoints so write checks happen before OS open."""
    gpu_mcp_guard.install_preopen_write_guards(APPROVED_WRITE_ROOTS)


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
    return gpu_mcp_guard.run_job(
        script_arg,
        [str(arg) for arg in script_args],
        repo_root=REPO_ROOT,
        script_roots=APPROVED_SCRIPT_ROOTS,
        write_roots=APPROVED_WRITE_ROOTS,
    )


def _build_python_gpu_argv(
    script_path: str,
    args: Optional[list[str]] = None,
    execution_host: Optional[str] = None,
) -> list[str]:
    """Build argv for an approved script launched through the guarded runner."""
    script = _validate_python_script_path(script_path)
    issues = scan_python_gpu_script_safety(str(script))
    if issues:
        raise ValueError(_script_rejection_message(script, issues))
    if execution_host is not None and not _is_local_host(execution_host):
        runner = _ensure_staged_safe_runner()
        return [
            PYTHON,
            str(runner),
            "--job",
            str(script),
            "--repo-root",
            str(REPO_ROOT),
            "--script-roots",
            json.dumps([str(path) for path in APPROVED_SCRIPT_ROOTS]),
            "--write-roots",
            json.dumps([str(path) for path in APPROVED_WRITE_ROOTS]),
            "--",
        ] + [str(arg) for arg in (args or [])]
    return [
        PYTHON,
        str(Path(__file__).resolve()),
        "--config",
        str(Path(GPU_MCP_CONFIG_PATH).expanduser().resolve()),
        "--safe-run",
        str(script),
        "--",
    ] + [str(arg) for arg in (args or [])]


def _remote_async_launch_command(argv: list[str], out_path: Path, env_values: dict[str, str]) -> str:
    """Build a remote launcher that opens async output with O_NOFOLLOW."""
    wrapper = (
        "import json, os, subprocess, sys\n"
        "out_path, cwd, argv_json, env_json = sys.argv[1:5]\n"
        "flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC\n"
        "if hasattr(os, 'O_NOFOLLOW'):\n"
        "    flags |= os.O_NOFOLLOW\n"
        "fd = os.open(out_path, flags, 0o600)\n"
        "env = os.environ.copy()\n"
        "env.update(json.loads(env_json))\n"
        "proc = subprocess.Popen(json.loads(argv_json), stdout=fd, stderr=subprocess.STDOUT, "
        "stdin=subprocess.DEVNULL, env=env, cwd=cwd, start_new_session=True)\n"
        "os.close(fd)\n"
        "print(proc.pid)\n"
    )
    return shlex.join(
        [
            PYTHON,
            "-c",
            wrapper,
            str(out_path),
            str(REPO_ROOT),
            json.dumps(argv),
            json.dumps(env_values),
        ]
    )


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
    "across configured hosts, run approved Python files on specific GPUs, "
    "identify which processes own which GPUs, and signal only GPU_MCP_USER "
    "processes after fingerprint confirmation. kill_gpu_process is not a "
    "general cleanup or scheduling tool: use it only for a specific PID that "
    "the caller intends to stop. First inspect the target, read the owner, GPU, "
    "start time, process group, and command preview, then pass the returned "
    "fingerprint only if it is still clearly the intended process."
))


@mcp.tool()
def preview_policy_reload():
    """Validate changed gpu-mcp.toml and preview an explicit policy reload.

    This does not activate the candidate policy. It returns a one-time reload
    token only after the file validates and the safety-relevant diff is shown
    to the human.
    """
    _cleanup_policy_reload_tokens()
    try:
        candidate = load_policy(CONFIG_POLICY.config_path)
    except ConfigError as exc:
        return json.dumps({
            "status": "error",
            "validation": "fail",
            "reason": str(exc),
        }, sort_keys=True)

    candidate_hash = policy_file_hash(candidate.config_path)
    diff_summary = diff_policy_summary(CONFIG_POLICY, candidate)
    token = "gpu-mcp-reload-v1:" + secrets.token_urlsafe(24)
    PENDING_POLICY_RELOADS[token] = {
        "candidate_hash": candidate_hash,
        "diff_summary": diff_summary,
        "created_at": time.monotonic(),
    }
    _cleanup_policy_reload_tokens()
    return json.dumps({
        "status": "preview",
        "validation": "pass",
        "config_path": str(candidate.config_path),
        "active_hash": ACTIVE_POLICY_HASH,
        "candidate_hash": candidate_hash,
        "diff_summary": diff_summary,
        "reload_token": token,
        "agent_instructions": POLICY_RELOAD_AGENT_INSTRUCTIONS,
    }, sort_keys=True)


@mcp.tool()
def reload_policy(token: str):
    """Activate a previously previewed and human-approved policy reload."""
    global ACTIVE_POLICY_HASH
    global _STALE_POLICY_HASH_CACHE

    if not isinstance(token, str) or not token:
        return json.dumps({
            "status": "refused",
            "reason": "reload token is required",
        }, sort_keys=True)
    pending, token_error = _pop_pending_policy_reload(token)
    if pending is None:
        return json.dumps({
            "status": "refused",
            "reason": token_error,
        }, sort_keys=True)

    try:
        candidate = load_policy(CONFIG_POLICY.config_path)
    except ConfigError as exc:
        return json.dumps({
            "status": "error",
            "validation": "fail",
            "reason": str(exc),
        }, sort_keys=True)

    candidate_hash = policy_file_hash(candidate.config_path)
    if candidate_hash != pending["candidate_hash"]:
        return json.dumps({
            "status": "refused",
            "reason": "policy changed after preview; preview again",
        }, sort_keys=True)

    approve_policy(candidate, diff_summary=list(pending["diff_summary"]))
    _apply_policy(candidate)
    ACTIVE_POLICY_HASH = candidate_hash
    stat = CONFIG_POLICY.config_path.stat()
    _STALE_POLICY_HASH_CACHE = {
        "signature": (stat.st_mtime_ns, stat.st_size),
        "hash": ACTIVE_POLICY_HASH,
    }
    return json.dumps({
        "status": "reloaded",
        "config_path": str(candidate.config_path),
        "active_hash": ACTIVE_POLICY_HASH,
        "diff_summary": pending["diff_summary"],
    }, sort_keys=True)


@mcp.tool()
def reject_policy_reload(token: str):
    """Discard a previously previewed policy reload token without changing policy."""
    if not isinstance(token, str) or not token:
        return json.dumps({
            "status": "refused",
            "reason": "reload token is required",
        }, sort_keys=True)
    pending, token_error = _pop_pending_policy_reload(token)
    if pending is None:
        return json.dumps({
            "status": "refused",
            "reason": token_error,
        }, sort_keys=True)
    return json.dumps({
        "status": "rejected",
        "active_hash": ACTIVE_POLICY_HASH,
        "reason": "reload token discarded; active policy unchanged",
    }, sort_keys=True)


@mcp.tool()
def check_gpus(
    samples: int = 2,
    threshold: int = 10,
):
    """Check GPU availability across the cluster.

    SSHes to configured nodes, takes multiple nvidia-smi samples, averages
    utilization, applies repo policy GPU-name/free-memory filters, and reports
    AVAILABLE/BUSY.

    Args:
        samples: Number of utilization samples to average (default 2).
        threshold: GPU utilization % at or below which a GPU is marked AVAILABLE (default 10).

    Returns:
        Formatted report of GPU status across the cluster.
    """
    if stale := _stale_policy_refusal():
        return stale
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 1:
        return "ERROR: samples must be a positive integer"
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        return "ERROR: threshold must be a non-negative integer"
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
            reason = _host_run_error("local" if local_host else host, query)
            suffix = f": {reason}" if reason else ""
            lines.append(f"  ssh/nvidia-smi failed{suffix}")
            continue
        if not local_host:
            remote_successes += 1

        ngpus = max(len(sample) for sample in sample_sets)
        for gpu_index in range(ngpus):
            seen = [sample[gpu_index] for sample in sample_sets if gpu_index < len(sample)]
            if not seen:
                continue
            util_values = [g["utilization_pct"] for g in seen if g["utilization_pct"] is not None]
            avg_util = sum(util_values) / len(util_values) if util_values else None
            last = seen[-1]
            if CONFIG_POLICY.allowed_gpu_names and not any(
                name in last["name"] for name in CONFIG_POLICY.allowed_gpu_names
            ):
                continue
            mem_free = (
                int(last["memory_total_MiB"]) - int(last["memory_used_MiB"])
                if last["memory_total_MiB"] is not None and last["memory_used_MiB"] is not None
                else None
            )
            status = (
                "AVAILABLE"
                if avg_util is not None
                and avg_util <= threshold
                and (mem_free is None or mem_free >= CONFIG_POLICY.min_free_memory_mib)
                else "BUSY"
            )
            mem_text = (
                f"{last['memory_used_MiB']}/{last['memory_total_MiB']} MiB"
                if last["memory_total_MiB"] is not None
                else f"{last['memory_used_MiB']} MiB"
            )
            util_text = f"{avg_util:.1f}%" if avg_util is not None else "N/A"
            lines.append(
                f"  GPU {last['index']} | {last['name']} | util_avg={util_text} | "
                f"mem={mem_text} | {status}"
            )

    if remote_successes == 0:
        lines.append(
            "WARNING: no non-local SSH GPU host succeeded; remote MCP SSH "
            "routing is not installed from this control host."
        )

    return "\n".join(lines) if len(lines) > 1 else "(no configured GPU nodes available)"


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
    if stale := _stale_policy_refusal():
        return stale
    if hosts is not None and (
        not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts)
    ):
        return "ERROR: hosts must be a list of host strings"
    if user_filter is not None and not isinstance(user_filter, str):
        return "ERROR: user_filter must be a string or null"
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
            reason = _host_run_error(host, proc_query) or _host_run_error(host, uuid_query)
            suffix = f": {reason}" if reason else ""
            lines.append(f"[{host}] SSH/nvidia-smi failed{suffix}")
            continue

        # Build UUID → index map
        uuid_to_idx = {}
        for row in uuid_raw.strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 2:
                parsed_index = _parse_optional_int(parts[0])
                if parsed_index is not None:
                    uuid_to_idx[parts[1]] = parsed_index

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
            ps_raw = _host_run(host, f"ps -p {pid} -o user=,args=")
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
    if stale := _stale_policy_refusal():
        return stale
    if isinstance(pid, bool):
        return json.dumps({
            "status": "refused",
            "host": host,
            "pid": str(pid),
            "killable": False,
            "reason": "pid must be an integer",
            "signal_sent": None,
        }, sort_keys=True)
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
        host: Configured hostname.
        gpu_index: GPU device index to use (sets CUDA_VISIBLE_DEVICES).
        script_path: Existing .py file under an approved script root.
        args: Positional CLI args passed to the Python script.
        async_mode: If True, run in background and return immediately with PID.
        output_file: Approved path for stdout/stderr capture (used with async_mode).

    Returns:
        Command output (sync) or PID info (async).
    """
    if stale := _stale_policy_refusal():
        return stale
    if not isinstance(gpu_index, int) or isinstance(gpu_index, bool) or gpu_index < 0:
        return "ERROR: gpu_index must be a non-negative integer"
    if args is not None and (
        not isinstance(args, list)
        or any(not isinstance(arg, (str, int, float, bool)) or arg is None for arg in args)
    ):
        return "ERROR: args must be a list of string/number/boolean values"
    if not isinstance(async_mode, bool):
        return "ERROR: async_mode must be a boolean"
    if output_file is not None and not isinstance(output_file, str):
        return "ERROR: output_file must be a string path"
    if not _is_allowed_host(host):
        return "ERROR: host must be one of the configured GPU MCP NODES"

    try:
        argv = _build_python_gpu_argv(script_path, args=args, execution_host=host)
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
            _prepare_output_parent(out_path)
        except ValueError as e:
            return f"ERROR: {e}"
        if _is_local_host(host):
            try:
                env = os.environ.copy()
                env.update(job_env)
                out_handle = _open_output_no_follow(out_path)
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
        launch_cmd = _remote_async_launch_command(argv, out_path, job_env)
        bg_cmd = _remote_repo_command(launch_cmd)
        pid_str = _ssh_run(host, bg_cmd)
        if pid_str is None:
            return f"ERROR: Failed to SSH to {host}"
        pid_value = pid_str.strip()
        if not pid_value.isdigit() or int(pid_value) <= 0:
            return f"ERROR: invalid async pid returned from {host}: {pid_value!r}"
        return json.dumps({
            "status": "launched",
            "host": host,
            "gpu_index": gpu_index,
            "pid": pid_value,
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
            result = getattr(e, "result", None)
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            details = f"{stdout}{stderr}"
            return f"ERROR: {e}" + (f"\n{details}" if details else "")


@mcp.tool()
def cluster_info():
    """Get a quick overview of the entire cluster: which nodes are reachable, GPU counts, load.

    Returns:
        Summary table of all cluster nodes.
    """
    if stale := _stale_policy_refusal():
        return stale
    gpu_query = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits"
    )
    load_query = "uptime"
    lines = ["HOST                | STATUS    | GPUs | GPU_UTIL_AVG | LOAD_AVG"]
    lines.append("-" * 72)

    for node in NODES:
        user, host = _node_user_host(node)
        gpu_raw = _host_run(host, gpu_query, user=user)
        if gpu_raw is None:
            reason = _host_run_error("local" if _is_local_host(host) else host, gpu_query)
            suffix = f" ({reason})" if reason else ""
            lines.append(f"{host:20s} | OFFLINE   |    - |            - | -{suffix}")
            continue
        load_raw = _host_run(host, load_query, user=user) or ""

        gpus = _parse_nvsmi_csv(gpu_raw) if gpu_raw else []
        util_values = [g["utilization_pct"] for g in gpus if g["utilization_pct"] is not None]
        avg_util = sum(util_values) // len(util_values) if util_values else None

        # Parse load average from uptime
        load_match = re.search(r"load average:\s*([\d.]+)", load_raw)
        load_avg = load_match.group(1) if load_match else "?"

        lines.append(
            f"{host:20s} | ONLINE    | {len(gpus):4d} | "
            f"{str(avg_util) + '%' if avg_util is not None else 'N/A':>10s} | {load_avg}"
        )

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
