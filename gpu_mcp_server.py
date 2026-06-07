#!/usr/bin/env python3
"""
MCP server for GPU cluster inspection and constrained Python execution.

Communicates via stdio and uses SSH/Fabric to inspect GPU availability or run
approved Python entrypoints on remote GPU hosts.

Usage:
    python gpu_mcp_server.py --config /absolute/path/to/gpu-mcp.toml

Register in Codex or another MCP-aware client as a stdio MCP server.
"""

import sys, os, json, time, subprocess, re, shlex, hashlib, signal as signal_lib, secrets, shutil, threading, atexit
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import gpu_mcp_guard
import gpu_mcp_reservations as reservations
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
GPU_MCP_TEST_CONTROL_FILE = os.environ.get("GPU_MCP_TEST_CONTROL_FILE", "").strip()
GPU_MCP_TEST_FAKE_BIN = os.environ.get("GPU_MCP_TEST_FAKE_BIN", "").strip()
GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS = (
    os.environ.get("GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS", "").strip().lower()
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
SERVER_INSTANCE_ID = reservations.generate_server_instance_id()

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
    "Do not revert the file. Do not edit Codex config or unrelated files.\n"
    "Stop immediately and explain to the human what you were trying to do, "
    "what changed, and why GPU MCP refused to continue.\n"
    "If the human intentionally changed the policy, the next step is "
    "preview_policy_reload. Show the safety diff and call reload_policy only "
    "after explicit human approval.\n"
    "Only edit gpu-mcp.toml while stale after explicit human rejection or "
    "cancellation of the prior candidate and explicit human re-orientation "
    "to the next candidate edit."
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


def _canonical_policy_host(host: str) -> str:
    """Return the canonical policy node host for an allowed input host alias."""
    requested = host.split("@")[-1].strip().lower()
    exact_matches: list[str] = []
    short_matches: list[str] = []
    for node in NODES:
        listed = node.split("@")[-1].strip().lower()
        if requested == listed:
            exact_matches.append(listed)
        elif requested == listed.split(".", 1)[0]:
            short_matches.append(listed)
    if len(exact_matches) == 1:
        return reservations.canonical_host_component(exact_matches[0])
    if len(short_matches) == 1:
        return reservations.canonical_host_component(short_matches[0])
    if len(short_matches) > 1:
        raise ValueError(f"host alias {host!r} is ambiguous in GPU MCP NODES")
    raise ValueError("host must be one of the configured GPU MCP NODES")


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
        if GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS and GPU_MCP_TEST_FAKE_BIN and argv:
            fake = Path(GPU_MCP_TEST_FAKE_BIN).expanduser() / argv[0]
            if fake.exists() and not fake.is_symlink():
                argv[0] = str(fake)
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


def _managed_supervisor_argv(
    argv: list[str],
    out_path: Path,
    env_values: dict[str, str],
    outcome_path: Path,
    outcome_base: dict,
) -> list[str]:
    wrapper = (
        "import json, os, signal, subprocess, sys, time, traceback\n"
        "out_path, cwd, argv_json, env_json, outcome_path, outcome_base_json = sys.argv[1:7]\n"
        "flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC\n"
        "if hasattr(os, 'O_NOFOLLOW'):\n"
        "    flags |= os.O_NOFOLLOW\n"
        "os.makedirs(os.path.dirname(outcome_path), mode=0o700, exist_ok=True)\n"
        "env = os.environ.copy()\n"
        "env.update(json.loads(env_json))\n"
        "out_fd = os.open(out_path, flags, 0o600)\n"
        "started_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())\n"
        "rc = None\n"
        "error_summary = None\n"
        "proc = None\n"
        "def forward_signal(signum, frame):\n"
        "    if proc is not None and proc.poll() is None:\n"
        "        try:\n"
        "            os.killpg(proc.pid, signum)\n"
        "        except ProcessLookupError:\n"
        "            pass\n"
        "        except Exception:\n"
        "            traceback.print_exc(file=sys.stderr)\n"
        "signal.signal(signal.SIGTERM, forward_signal)\n"
        "signal.signal(signal.SIGINT, forward_signal)\n"
        "try:\n"
        "    proc = subprocess.Popen(json.loads(argv_json), stdout=out_fd, stderr=subprocess.STDOUT, "
        "stdin=subprocess.DEVNULL, env=env, cwd=cwd, start_new_session=True)\n"
        "    rc = proc.wait()\n"
        "except Exception as exc:\n"
        "    rc = 127\n"
        "    error_summary = str(exc)\n"
        "finally:\n"
        "    os.close(out_fd)\n"
        "ended_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())\n"
        "outcome = json.loads(outcome_base_json)\n"
        "outcome['remote_pid'] = os.getpid()\n"
        "outcome['started_at'] = started_at\n"
        "outcome['ended_at'] = ended_at\n"
        "outcome['error_summary'] = error_summary\n"
        "if rc == 0:\n"
        "    outcome['terminal_status'] = 'success'\n"
        "    outcome['exit_code'] = 0\n"
        "    outcome['signal'] = None\n"
        "elif rc < 0:\n"
        "    outcome['terminal_status'] = 'signaled'\n"
        "    outcome['exit_code'] = None\n"
        "    outcome['signal'] = str(-rc)\n"
        "else:\n"
        "    outcome['terminal_status'] = 'failure'\n"
        "    outcome['exit_code'] = rc\n"
        "    outcome['signal'] = None\n"
        "tmp = outcome_path + '.tmp.' + str(os.getpid())\n"
        "with open(tmp, 'w') as fh:\n"
        "    json.dump(outcome, fh, sort_keys=True)\n"
        "    fh.write('\\n')\n"
        "os.replace(tmp, outcome_path)\n"
    )
    return [
        PYTHON,
        "-c",
        wrapper,
        str(out_path),
        str(REPO_ROOT),
        json.dumps(argv),
        json.dumps(env_values),
        str(outcome_path),
        json.dumps(outcome_base),
    ]


def _remote_async_launch_command(
    argv: list[str],
    out_path: Path,
    env_values: dict[str, str],
    outcome_path: Path,
    outcome_base: dict,
    pid_ack_path: Path | None = None,
) -> str:
    """Build a remote managed launcher that writes an outcome record."""
    supervisor_argv = _managed_supervisor_argv(argv, out_path, env_values, outcome_path, outcome_base)
    if pid_ack_path is not None:
        wrapper = (
            "import json, os, subprocess, sys\n"
            "supervisor_argv = json.loads(sys.argv[1])\n"
            "pid_ack_path = sys.argv[2]\n"
            "os.makedirs(os.path.dirname(pid_ack_path), mode=0o700, exist_ok=True)\n"
            "ack_fh = open(pid_ack_path, 'w')\n"
            "try:\n"
            "    proc = subprocess.Popen(supervisor_argv, stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)\n"
            "except Exception:\n"
            "    ack_fh.close()\n"
            "    try:\n"
            "        os.unlink(pid_ack_path)\n"
            "    except FileNotFoundError:\n"
            "        pass\n"
            "    raise\n"
            "json.dump({'schema_version': 1, 'remote_pid': proc.pid}, ack_fh, sort_keys=True)\n"
            "ack_fh.write('\\n')\n"
            "ack_fh.flush()\n"
            "os.fsync(ack_fh.fileno())\n"
            "ack_fh.close()\n"
            "print(proc.pid)\n"
        )
        return " ".join([
            _shell_env_prefix(env_values),
            shlex.join([
                PYTHON,
                "-c",
                wrapper,
                json.dumps(supervisor_argv),
                str(pid_ack_path),
            ]),
        ])
    return " ".join([
        _shell_env_prefix(env_values),
        "nohup",
        shlex.join(supervisor_argv),
        ">/dev/null",
        "2>&1",
        "&",
        "echo",
        "$!",
    ])


def _remote_legacy_async_launch_command(argv: list[str], out_path: Path, env_values: dict[str, str]) -> str:
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


def _managed_process_fingerprint(job_id: str, attempt_id: str, host: str, nonce: str) -> str:
    payload = {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "host": reservations.canonical_host_component(host),
        "nonce": str(nonce),
        "server_instance_id": SERVER_INSTANCE_ID,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "gpu-mcp-process:" + hashlib.sha256(encoded).hexdigest()


def _launch_handle_response(
    *,
    status: str,
    job_id: str,
    attempt_id: str,
    reservation_key_value: str,
    host: str,
    gpu_index: int,
    output_path: Path,
    next_poll_after: str,
    job_lifecycle: str,
    launch: str,
    async_mode_requested: bool,
    process: dict | None = None,
    reason: str = "",
    heartbeat_interval_sec: int = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC,
    job_role: str | None = None,
    job_role_defaulted: bool | None = None,
    cadence_basis: dict | None = None,
) -> str:
    payload = {
        "status": status,
        "reason": reason,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": host,
        "gpu_index": gpu_index,
        "server_instance_id": SERVER_INSTANCE_ID,
        "process": process or {
            "remote_pid": None,
            "remote_start_time": None,
            "remote_boot_id": None,
            "process_fingerprint": None,
        },
        "output": {
            "path": str(output_path),
        },
        "next_poll_after": next_poll_after,
        "heartbeat_interval_sec": heartbeat_interval_sec,
        "job_lifecycle": job_lifecycle,
        "launch": launch,
        "async_mode_requested": async_mode_requested,
    }
    if job_role is not None:
        payload["job_role"] = job_role
    if job_role_defaulted is not None:
        payload["job_role_defaulted"] = job_role_defaulted
    if cadence_basis is not None:
        payload["cadence_basis"] = cadence_basis
    return _json_tool_response(payload)


class HeartbeatManager:
    """Per-task lease heartbeats owned by this MCP server instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._owned: dict[str, dict[str, object]] = {}
        self._unhealthy_reason = ""
        self.force_write_failure_for_tests = False

    def register(self, reservation_key_value: str, *, interval_sec: int) -> None:
        with self._lock:
            self._owned[reservation_key_value] = {
                "interval_sec": interval_sec,
                "last_attempt_monotonic": 0.0,
            }
            self._ensure_thread_locked()

    def unregister(self, reservation_key_value: str) -> None:
        with self._lock:
            self._owned.pop(reservation_key_value, None)

    def owned_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._owned)

    def is_healthy(self) -> bool:
        with self._lock:
            return not self._unhealthy_reason

    def health_reason(self) -> str:
        with self._lock:
            return self._unhealthy_reason

    def mark_unhealthy_for_tests(self, reason: str = "test heartbeat failure") -> None:
        with self._lock:
            self._unhealthy_reason = reason

    def clear_unhealthy_for_tests(self, *, prefix: str | None = None) -> None:
        with self._lock:
            if prefix is None or self._unhealthy_reason.startswith(prefix):
                self._unhealthy_reason = ""

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def heartbeat_once(self, reservation_key_value: str, *, now: str | None = None) -> bool:
        with self._lock:
            if reservation_key_value not in self._owned:
                return False
            interval = int(self._owned[reservation_key_value]["interval_sec"])
            if self.force_write_failure_for_tests:
                self._unhealthy_reason = "heartbeat write failed: test forced failure"
                return False
            try:
                self._write_heartbeat(reservation_key_value, interval_sec=interval, now=now)
            except Exception as exc:
                self._unhealthy_reason = f"heartbeat write failed: {exc}"
                return False
            self._unhealthy_reason = ""
            self._owned[reservation_key_value]["last_attempt_monotonic"] = time.monotonic()
            return True

    def update_owned_metadata(self, reservation_key_value: str, updates: dict[str, object]) -> None:
        with self._lock:
            if reservation_key_value not in self._owned:
                raise PermissionError("current server does not own this reservation")
            self._merge_owned_metadata(reservation_key_value, updates)
            if "heartbeat_interval_sec" in updates:
                self._owned[reservation_key_value]["interval_sec"] = int(updates["heartbeat_interval_sec"])

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="gpu-mcp-heartbeats",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=1.0):
            with self._lock:
                keys = list(self._owned)
            for key in keys:
                with self._lock:
                    item = self._owned.get(key)
                    if item is None:
                        continue
                    interval = int(item["interval_sec"])
                    last_attempt = float(item["last_attempt_monotonic"])
                    if time.monotonic() - last_attempt < max(1, min(interval, 60)):
                        continue
                self.heartbeat_once(key)

    def _write_heartbeat(self, reservation_key_value: str, *, interval_sec: int, now: str | None) -> None:
        self._merge_owned_metadata(
            reservation_key_value,
            {
                "last_heartbeat_at": now or reservations.iso_timestamp(),
                "heartbeat_interval_sec": interval_sec,
            },
        )

    def _merge_owned_metadata(self, reservation_key_value: str, updates: dict[str, object]) -> None:
        registry_root = reservations.reservation_registry_root()
        metadata_path = reservations.reservation_dir(registry_root, reservation_key_value) / "metadata.json"
        with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
            metadata = reservations.read_json_file_no_follow(metadata_path)
            if not isinstance(metadata, dict):
                raise ValueError("metadata JSON must be an object")
            if metadata.get("server_instance_id") != SERVER_INSTANCE_ID:
                raise PermissionError("current server does not own this reservation")
            metadata.update(updates)
            reservations.atomic_write_json(metadata_path, metadata)


HEARTBEAT_MANAGER = HeartbeatManager()
atexit.register(HEARTBEAT_MANAGER.stop)


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


def _process_gpu_usage(host: str, pid: int, *, timeout: int = 15) -> tuple[Optional[str], Optional[str]]:
    """Return GPU index and memory for a PID when nvidia-smi reports it."""
    proc_query = (
        "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_gpu_memory "
        "--format=csv,noheader,nounits"
    )
    uuid_query = "nvidia-smi --query-gpu=index,uuid --format=csv,noheader"
    proc_raw = _host_run(host, proc_query, timeout=timeout)
    uuid_raw = _host_run(host, uuid_query, timeout=timeout)
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
    ps_raw = _host_run(host, ps_cmd, timeout=2)
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


def _json_tool_response(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)


def _phase7_trace_path() -> Path | None:
    raw = os.environ.get("GPU_MCP_TEST_PHASE7_TRACE_FILE", "").strip()
    if not raw or "PYTEST_CURRENT_TEST" not in os.environ:
        return None
    return Path(raw).expanduser()


def _phase7_trace_append(event: str, *, tool: str, **fields: object) -> None:
    trace_path = _phase7_trace_path()
    if trace_path is None:
        return
    try:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        event_index = 0
        if trace_path.exists():
            event_index = sum(1 for _ in trace_path.open())
        payload = {
            "schema_version": 1,
            "run_id": os.environ.get("GPU_MCP_TEST_PHASE7_RUN_ID", "server"),
            "scenario": os.environ.get("GPU_MCP_TEST_PHASE7_SCENARIO", "server"),
            "event_index": event_index,
            "event": event,
            "tool": tool,
            **fields,
        }
        with trace_path.open("a") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception:
        return


def _phase7_trace_args(tool: str, kwargs: dict[str, object]) -> dict[str, object]:
    if tool == "run_python_on_gpu":
        return {
            "host": kwargs.get("host"),
            "gpu_index": kwargs.get("gpu_index"),
            "script_path": kwargs.get("script_path"),
            "async_mode": kwargs.get("async_mode"),
            "job_role": kwargs.get("job_role"),
            "smoke_job_id": kwargs.get("smoke_job_id"),
            "has_smoke_skip_reason": _nonempty_text(kwargs.get("smoke_skip_reason")) is not None,
            "expected_duration_sec": kwargs.get("expected_duration_sec"),
            "cadence_hint_sec": kwargs.get("cadence_hint_sec"),
        }
    if tool == "manage_gpu_job":
        return {
            "action": kwargs.get("action"),
            "job_id": kwargs.get("job_id"),
            "reservation_key": kwargs.get("reservation_key"),
            "has_early_poll_reason": _nonempty_text(kwargs.get("early_poll_reason")) is not None,
            "has_reason": _nonempty_text(kwargs.get("reason")) is not None,
            "expected_duration_sec": kwargs.get("expected_duration_sec"),
            "cadence_hint_sec": kwargs.get("cadence_hint_sec"),
        }
    return dict(kwargs)


def _phase7_trace_response_summary(raw_response: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_response)
    except Exception:
        return {"status": "unparseable"}
    if not isinstance(payload, dict):
        return {"status": "non_object"}
    keys = [
        "status",
        "action",
        "reason",
        "job_id",
        "attempt_id",
        "reservation_key",
        "job_role",
        "job_role_defaulted",
        "job_lifecycle",
        "heartbeat_interval_sec",
        "next_poll_after",
        "polling_state",
        "full_status_performed",
        "remote_inspection_performed",
        "early_poll_override_recorded",
        "cadence_basis",
    ]
    return {key: payload.get(key) for key in keys if key in payload}


def _phase7_trace_tool_call(tool: str, kwargs: dict[str, object]) -> None:
    _phase7_trace_append("tool_call", tool=tool, args=_phase7_trace_args(tool, kwargs))


def _phase7_trace_tool_result(tool: str, raw_response: str) -> None:
    _phase7_trace_append(
        "tool_result",
        tool=tool,
        response_summary=_phase7_trace_response_summary(raw_response),
    )


def _test_controls_enabled() -> bool:
    return GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS


def _apply_test_controls_from_file() -> None:
    """Apply pytest-only harness controls that are invisible to MCP clients."""
    if not _test_controls_enabled() or not GPU_MCP_TEST_CONTROL_FILE:
        return
    control_path = Path(GPU_MCP_TEST_CONTROL_FILE).expanduser()
    try:
        raw = reservations.read_json_file_no_follow(control_path)
    except FileNotFoundError:
        HEARTBEAT_MANAGER.clear_unhealthy_for_tests(prefix="test control:")
        return
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    reason = raw.get("heartbeat_unhealthy_reason")
    if isinstance(reason, str) and reason.strip():
        HEARTBEAT_MANAGER.mark_unhealthy_for_tests(f"test control: {reason.strip()}")
        remaining = raw.get("heartbeat_unhealthy_remaining")
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            raw["heartbeat_unhealthy_remaining"] = max(0, remaining - 1)
            if raw["heartbeat_unhealthy_remaining"] <= 0:
                raw["heartbeat_unhealthy_reason"] = None
            try:
                reservations.atomic_write_json(control_path, raw)
            except Exception:
                pass
    elif reason is None:
        HEARTBEAT_MANAGER.clear_unhealthy_for_tests(prefix="test control:")


def _launch_refusal(reason: str, **extra: object) -> str:
    payload = {
        "status": "refused",
        "reason": reason,
        "job_id": None,
        "reservation_key": extra.pop("reservation_key", None),
        "host": extra.pop("host", None),
        "gpu_index": extra.pop("gpu_index", None),
        "server_instance_id": SERVER_INSTANCE_ID,
    }
    payload.update(extra)
    return _json_tool_response(payload)


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_poll_after(interval_sec: int = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC) -> str:
    interval = max(
        reservations.MIN_HEARTBEAT_INTERVAL_SEC,
        min(reservations.MAX_HEARTBEAT_INTERVAL_SEC, int(interval_sec)),
    )
    return (
        datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=interval)
    ).isoformat().replace("+00:00", "Z")


def _next_poll_after_from(now: datetime, interval_sec: int) -> str:
    interval = max(
        reservations.MIN_HEARTBEAT_INTERVAL_SEC,
        min(reservations.MAX_HEARTBEAT_INTERVAL_SEC, int(interval_sec)),
    )
    return (
        now.astimezone(timezone.utc).replace(microsecond=0)
        + timedelta(seconds=interval)
    ).isoformat().replace("+00:00", "Z")


def _parse_next_poll_after(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z") or " " in value:
        return None
    return _parse_iso_timestamp(value)


def _nonempty_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _validate_positive_int(value: object, *, name: str) -> tuple[int | None, str]:
    if value is None:
        return None, ""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, f"{name} must be a positive integer number of seconds"
    return value, ""


def _clamp_cadence_interval(value: int) -> int:
    return max(
        reservations.MIN_HEARTBEAT_INTERVAL_SEC,
        min(reservations.MAX_HEARTBEAT_INTERVAL_SEC, int(value)),
    )


def _coarse_cadence_for_duration(duration_sec: int) -> int:
    if duration_sec <= 300:
        return 60
    if duration_sec <= 1800:
        return 180
    if duration_sec <= 7200:
        return 600
    return 1800


def _outcome_runtime_sec(outcome: dict | None) -> int | None:
    if not isinstance(outcome, dict):
        return None
    started = _parse_iso_timestamp(outcome.get("started_at"))
    ended = _parse_iso_timestamp(outcome.get("ended_at"))
    if started is None or ended is None:
        return None
    return max(0, int((ended - started).total_seconds()))


def _resolve_job_role(job_role: object, async_mode: bool | None) -> tuple[str | None, bool, str]:
    if job_role is not None:
        if not isinstance(job_role, str):
            return None, False, "job_role must be one of: smoke, main, one_off"
        normalized = job_role.strip().lower()
        if normalized not in {"smoke", "main", "one_off"}:
            return None, False, "job_role must be one of: smoke, main, one_off"
        return normalized, False, ""
    return ("main" if async_mode is True else "one_off"), True, ""


def _smoke_evidence(smoke_job_id: object) -> tuple[dict | None, dict]:
    basis: dict[str, object] = {
        "source": "smoke_job",
        "smoke_job_id": smoke_job_id,
        "positive_viability_evidence": False,
    }
    if smoke_job_id is None:
        return None, basis
    if not reservations.is_job_id(smoke_job_id):
        basis["smoke_lookup_status"] = "invalid_job_id"
        return None, basis
    record = _read_repo_job_record(str(smoke_job_id))
    if record is None:
        basis["smoke_lookup_status"] = "missing"
        return None, basis
    if record.get("job_role") != "smoke":
        basis["smoke_lookup_status"] = "wrong_role"
        return None, basis
    outcome = _read_outcome_record(record)
    lifecycle = record.get("job_lifecycle")
    if outcome is not None:
        terminal = outcome.get("terminal_status")
        lifecycle = "succeeded" if terminal == "success" else "failed"
    basis["smoke_lifecycle"] = lifecycle
    if lifecycle != "succeeded":
        basis["smoke_lookup_status"] = "not_successful"
        return None, basis
    runtime_sec = _outcome_runtime_sec(outcome)
    if runtime_sec is not None:
        basis["smoke_runtime_sec"] = runtime_sec
    basis["positive_viability_evidence"] = True
    basis["smoke_lookup_status"] = "ok"
    return {"record": record, "outcome": outcome, "runtime_sec": runtime_sec}, basis


def _phase7_cadence_basis(
    *,
    job_role: str,
    expected_duration_sec: int | None,
    cadence_hint_sec: int | None,
    smoke_job_id: object,
    smoke_cadence_representative: bool,
    smoke_skip_reason: str | None,
) -> tuple[int, dict, bool]:
    smoke, smoke_basis = _smoke_evidence(smoke_job_id)
    positive_smoke = bool(smoke_basis.get("positive_viability_evidence"))
    smoke_fields = {key: value for key, value in smoke_basis.items() if key != "source"}
    if cadence_hint_sec is not None:
        selected = _clamp_cadence_interval(cadence_hint_sec)
        basis: dict[str, object] = {
            "source": "cadence_hint",
            "cadence_hint_sec": cadence_hint_sec,
            "selected_interval_sec": selected,
            "positive_viability_evidence": positive_smoke,
        }
        if expected_duration_sec is not None:
            basis["expected_duration_sec"] = expected_duration_sec
        if smoke_job_id is not None:
            basis.update(smoke_fields)
        return selected, basis, positive_smoke
    if expected_duration_sec is not None:
        selected = _clamp_cadence_interval(_coarse_cadence_for_duration(expected_duration_sec))
        basis = {
            "source": "expected_duration",
            "expected_duration_sec": expected_duration_sec,
            "selected_interval_sec": selected,
            "positive_viability_evidence": positive_smoke,
        }
        if smoke_job_id is not None:
            basis.update(smoke_fields)
        return selected, basis, positive_smoke
    if smoke_job_id is not None:
        basis = dict(smoke_basis)
        basis["smoke_cadence_representative"] = bool(smoke_cadence_representative)
        if positive_smoke and smoke_cadence_representative and smoke and smoke.get("runtime_sec") is not None:
            selected = _clamp_cadence_interval(_coarse_cadence_for_duration(int(smoke["runtime_sec"])))
            basis["cadence_evidence_used"] = True
            basis["selected_interval_sec"] = selected
            return selected, basis, positive_smoke
        basis["cadence_evidence_used"] = False
        basis["conservative_reason"] = "smoke_observation_only"
        basis["selected_interval_sec"] = reservations.MIN_HEARTBEAT_INTERVAL_SEC
        return reservations.MIN_HEARTBEAT_INTERVAL_SEC, basis, positive_smoke
    basis = {
        "source": "conservative_no_evidence",
        "selected_interval_sec": reservations.MIN_HEARTBEAT_INTERVAL_SEC,
        "positive_viability_evidence": False,
    }
    if smoke_skip_reason is not None:
        basis["conservative_reason"] = "smoke_skipped"
        basis["skip_reason_recorded"] = True
    else:
        basis["conservative_reason"] = "missing_cadence_evidence"
    return reservations.MIN_HEARTBEAT_INTERVAL_SEC, basis, False


def _reservation_age_fields(metadata: dict, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    last = _parse_iso_timestamp(metadata.get("last_heartbeat_at"))
    interval_raw = metadata.get("heartbeat_interval_sec")
    try:
        interval = int(interval_raw)
    except (TypeError, ValueError):
        interval = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC
    if last is None:
        return {
            "heartbeat_age_sec": None,
            "heartbeat_interval_sec": interval,
            "heartbeat_stale": True,
        }
    age = max(0.0, (current - last).total_seconds())
    return {
        "heartbeat_age_sec": age,
        "heartbeat_interval_sec": interval,
        "heartbeat_stale": age > interval * reservations.STALE_MULTIPLIER,
    }


def _reservation_row_from_error(key: str, reason: str) -> dict:
    return {
        "job_id": None,
        "reservation_key": key,
        "host": None,
        "gpu_index": None,
        "script_name": None,
        "reservation_state": "UNKNOWN_RESERVED",
        "computed_state": "UNKNOWN_RESERVED",
        "metadata_status": "unreadable",
        "metadata_error": reason,
        "heartbeat": None,
        "server_instance_id": None,
        "owned_by_current_server": False,
        "allowed_actions": ["status"],
        "last_inspection": None,
    }


def _safe_script_name(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    name = Path(value).name
    if name in {"", ".", ".."}:
        return None
    return name


def _safe_host_from_metadata(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return reservations.canonical_host_component(value)
    except ValueError:
        return None


def _reservation_row_from_metadata(metadata: dict, *, key: str) -> dict:
    heartbeat = _reservation_age_fields(metadata)
    state = "STALE_RESERVED" if heartbeat["heartbeat_stale"] else "RESERVED"
    owned = (
        metadata.get("server_instance_id") == SERVER_INSTANCE_ID
        and key in HEARTBEAT_MANAGER.owned_keys()
    )
    return {
        "job_id": metadata.get("job_id") if reservations.is_job_id(metadata.get("job_id")) else None,
        "reservation_key": (
            metadata.get("reservation_key")
            if reservations.is_reservation_key(metadata.get("reservation_key"))
            else key
        ),
        "host": _safe_host_from_metadata(metadata.get("host")),
        "gpu_index": metadata.get("gpu_index") if isinstance(metadata.get("gpu_index"), int) else None,
        "script_name": _safe_script_name(metadata.get("script_name")),
        "reservation_state": state,
        "computed_state": state,
        "metadata_status": "ok",
        "metadata_error": "",
        "heartbeat": heartbeat,
        "server_instance_id": metadata.get("server_instance_id")
        if reservations.is_server_instance_id(metadata.get("server_instance_id"))
        else None,
        "owned_by_current_server": owned,
        "allowed_actions": (
            ["status", "stop", "retry", "finish"]
            if owned and HEARTBEAT_MANAGER.is_healthy()
            else ["status"]
        ),
        "last_inspection": None,
    }


def _load_reservation_rows(scope: str = "all") -> tuple[str, str, dict[str, dict]]:
    registry_root = reservations.reservation_registry_root()
    rows: dict[str, dict] = {}
    try:
        entries = sorted(registry_root.iterdir()) if registry_root.exists() else []
    except OSError as exc:
        return "unavailable", str(exc), rows

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_dir():
            rows[entry.name] = _reservation_row_from_error(entry.name, "reservation entry is not a real directory")
            continue
        key = entry.name
        metadata_path = entry / "metadata.json"
        try:
            metadata = reservations.read_json_file_no_follow(metadata_path)
            if not isinstance(metadata, dict):
                raise ValueError("metadata JSON must be an object")
        except Exception as exc:
            rows[key] = _reservation_row_from_error(key, str(exc))
            continue
        if scope == "mine" and str(metadata.get("repo")) != str(REPO_ROOT):
            continue
        rows[key] = _reservation_row_from_metadata(metadata, key=key)
    return "ok", "", rows


def _read_reservation_metadata(reservation_key_value: str) -> dict | None:
    try:
        metadata = reservations.read_json_file_no_follow(
            reservations.reservation_dir(
                reservations.reservation_registry_root(),
                reservation_key_value,
            )
            / "metadata.json"
        )
    except Exception:
        return None
    return metadata if isinstance(metadata, dict) else None


def _metadata_stale(metadata: dict) -> bool:
    return bool(_reservation_age_fields(metadata)["heartbeat_stale"])


def _reservation_key_parts(reservation_key_value: str) -> tuple[str, int] | None:
    if not reservations.is_reservation_key(reservation_key_value):
        return None
    host, gpu_text = reservation_key_value.rsplit(".gpu", 1)
    return host, int(gpu_text)


def _metadata_matches_reservation_key(reservation_key_value: str, metadata: dict) -> bool:
    parts = _reservation_key_parts(reservation_key_value)
    if parts is None:
        return False
    host, gpu_index = parts
    try:
        metadata_host = reservations.canonical_host_component(str(metadata.get("host")))
    except ValueError:
        return False
    return (
        metadata.get("reservation_key") == reservation_key_value
        and metadata_host == host
        and metadata.get("gpu_index") == gpu_index
    )


def _process_start_time(host: str, pid: int) -> str | None:
    for _ in range(5):
        raw = _host_run(host, f"ps -p {pid} -o lstart=", timeout=1)
        if raw is not None:
            value = raw.strip()
            if value:
                return value
        time.sleep(0.05)
    return None


def _inspect_reservation_process(metadata: dict) -> dict:
    """Return whether the original process is alive, gone, or unknown.

    This is the central process-proof boundary. Tests may monkeypatch it with
    precise fake process-table facts; production uses conservative ps/nvidia-smi
    probes and treats missing identity as unknown rather than available.
    """
    pid = metadata.get("remote_pid")
    host = metadata.get("host")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {
            "status": "unknown",
            "reason": "reservation has no recorded remote_pid; launch outcome is unknown",
            "remote_pid": pid,
        }
    if not isinstance(host, str) or not _is_allowed_host(host):
        return {
            "status": "unknown",
            "reason": "reservation host is not inspectable under active policy",
            "remote_pid": pid,
        }
    ps_cmd = f"ps -p {pid} -o pid= -o user= -o stat= -o lstart="
    ps_raw = _host_run(host, ps_cmd, timeout=2)
    if ps_raw is None:
        reason = _host_run_error(host, ps_cmd) or "process inspection failed"
        return {"status": "unknown", "reason": reason, "remote_pid": pid}
    if not ps_raw.strip():
        return {"status": "gone", "reason": "process not found", "remote_pid": pid}
    fields = ps_raw.split(None, 3)
    if len(fields) < 4:
        return {"status": "unknown", "reason": "malformed ps output", "remote_pid": pid}
    _, owner, state, start_time = fields[:4]
    if owner != metadata.get("owner_user"):
        return {
            "status": "gone",
            "reason": "process owner mismatch; original process is gone",
            "remote_pid": pid,
        }
    if "Z" in state:
        return {"status": "gone", "reason": "process is zombie", "remote_pid": pid}
    expected_start = metadata.get("remote_start_time")
    if expected_start and start_time.strip() != str(expected_start).strip():
        return {"status": "gone", "reason": "process start time mismatch", "remote_pid": pid}
    expected_boot = metadata.get("remote_boot_id")
    if expected_boot:
        boot_raw = _host_run(host, "cat /proc/sys/kernel/random/boot_id", timeout=1)
        if boot_raw is None:
            return {"status": "unknown", "reason": "host boot id could not be inspected", "remote_pid": pid}
        if boot_raw.strip() != str(expected_boot).strip():
            return {"status": "gone", "reason": "host boot id mismatch", "remote_pid": pid}
    expected_fingerprint = metadata.get("process_fingerprint")
    if expected_fingerprint:
        env_cmd = (
            f"{shlex.quote(PYTHON)} -c "
            + shlex.quote(
                "import os,sys;"
                "p=f'/proc/{}/environ'.format(sys.argv[1]);"
                "data=open(p,'rb').read().split(b'\\0');"
                "print(next((x.split(b'=',1)[1].decode() for x in data "
                "if x.startswith(b'GPU_MCP_PROCESS_FINGERPRINT=')), ''))"
            )
            + f" {pid}"
        )
        observed_fingerprint = _host_run(host, env_cmd, timeout=1)
        if observed_fingerprint is None:
            reservation_key_value = metadata.get("reservation_key")
            current_owner_start_match = (
                metadata.get("server_instance_id") == SERVER_INSTANCE_ID
                and reservations.is_reservation_key(reservation_key_value)
                and str(reservation_key_value) in HEARTBEAT_MANAGER.owned_keys()
                and bool(expected_start)
            )
            if current_owner_start_match:
                gpu_index, gpu_memory = _process_gpu_usage(host, pid, timeout=1)
                return {
                    "status": "alive",
                    "reason": "matching current-owner process exists; fingerprint env unreadable but start time matched",
                    "remote_pid": pid,
                    "gpu_index": gpu_index,
                    "gpu_memory_mib": gpu_memory,
                }
            return {"status": "unknown", "reason": "process fingerprint could not be inspected", "remote_pid": pid}
        if observed_fingerprint.strip() != str(expected_fingerprint):
            return {"status": "gone", "reason": "process fingerprint mismatch", "remote_pid": pid}
    gpu_index, gpu_memory = _process_gpu_usage(host, pid, timeout=1)
    return {
        "status": "alive",
        "reason": "matching process exists",
        "remote_pid": pid,
        "gpu_index": gpu_index,
        "gpu_memory_mib": gpu_memory,
    }


def _cleanup_stale_gone_reservation(reservation_key_value: str, metadata: dict) -> tuple[bool, dict]:
    inspection = _inspect_reservation_process(metadata)
    if inspection["status"] != "gone":
        return False, inspection
    registry_root = reservations.reservation_registry_root()
    try:
        with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
            latest = _read_reservation_metadata(reservation_key_value)
            if latest is None:
                return False, {"status": "unknown", "reason": "reservation disappeared during cleanup"}
            if not _metadata_stale(latest):
                return False, {"status": "alive", "reason": "heartbeat refreshed during cleanup"}
            if (
                latest.get("job_id") != metadata.get("job_id")
                or latest.get("remote_pid") != metadata.get("remote_pid")
                or latest.get("server_instance_id") != metadata.get("server_instance_id")
                or not _metadata_matches_reservation_key(reservation_key_value, latest)
            ):
                return False, {"status": "alive", "reason": "metadata changed during cleanup"}
            target = reservations.quarantine_reservation(
                registry_root=registry_root,
                reservation_key_value=reservation_key_value,
                suffix=reservations.utc_timestamp(),
            )
            return True, {
                "status": "gone",
                "reason": "stale reservation quarantined after process-gone proof",
                "quarantine_path": str(target),
            }
    except FileNotFoundError:
        return False, {"status": "gone", "reason": "reservation already gone"}


def _refresh_stale_reservations(
    rows: dict[str, dict],
    *,
    max_total: int = 5,
    max_per_host: int = 2,
) -> dict[str, dict]:
    refreshed = dict(rows)
    inspected_total = 0
    inspected_by_host: dict[str, int] = {}
    for key, row in list(rows.items()):
        heartbeat = row.get("heartbeat") or {}
        if not heartbeat.get("heartbeat_stale"):
            continue
        host = str(row.get("host") or "")
        if inspected_total >= max_total or inspected_by_host.get(host, 0) >= max_per_host:
            row = dict(row)
            row["reservation_state"] = "UNKNOWN_RESERVED"
            row["computed_state"] = "UNKNOWN_RESERVED"
            row["last_inspection"] = {
                "status": "skipped",
                "reason": "stale inspection budget exhausted",
            }
            refreshed[key] = row
            continue
        metadata = _read_reservation_metadata(key)
        if metadata is None:
            refreshed[key] = _reservation_row_from_error(key, "metadata unavailable during stale refresh")
            continue
        if not _metadata_matches_reservation_key(key, metadata):
            row = dict(row)
            row["reservation_state"] = "UNKNOWN_RESERVED"
            row["computed_state"] = "UNKNOWN_RESERVED"
            row["last_inspection"] = {
                "status": "unknown",
                "reason": "metadata does not match reservation key",
            }
            refreshed[key] = row
            continue
        if not isinstance(metadata.get("remote_pid"), int):
            row = dict(row)
            row["reservation_state"] = "UNKNOWN_RESERVED"
            row["computed_state"] = "UNKNOWN_RESERVED"
            row["last_inspection"] = {
                "status": "unknown",
                "reason": "stale reservation has no recorded remote_pid",
            }
            refreshed[key] = row
            continue
        inspected_total += 1
        inspected_by_host[host] = inspected_by_host.get(host, 0) + 1
        cleaned, inspection = _cleanup_stale_gone_reservation(key, metadata)
        if cleaned:
            refreshed.pop(key, None)
            continue
        row = _reservation_row_from_metadata(metadata, key=key)
        row["last_inspection"] = inspection
        if inspection["status"] == "unknown":
            row["reservation_state"] = "UNKNOWN_RESERVED"
            row["computed_state"] = "UNKNOWN_RESERVED"
        elif inspection["status"] == "alive":
            row["reservation_state"] = "STALE_RESERVED"
            row["computed_state"] = "STALE_RESERVED"
        refreshed[key] = row
    return refreshed


def _empty_status_response(*, action: str, reason: str) -> str:
    return _json_tool_response({
        "status": "no_target" if action == "status" else "refused",
        "action": action,
        "reason": reason,
        "job_id": None,
        "reservation_key": None,
        "reservation_state": None,
        "job_lifecycle": None,
        "owned_by_current_server": False,
        "heartbeat": None,
        "process": None,
        "allowed_actions": ["status"],
        "output": None,
        "next_poll_after": None,
        "server_instance_id": SERVER_INSTANCE_ID,
    })


def _read_repo_job_record(job_id: str) -> dict | None:
    try:
        record = reservations.read_json_file_no_follow(reservations.job_record_path(REPO_ROOT, job_id))
    except Exception:
        return None
    return record if isinstance(record, dict) else None


def _job_record_matches_current_metadata(record: dict, metadata: dict | None) -> bool:
    if metadata is None:
        return False
    reservation_key_value = record.get("reservation_key")
    return (
        reservations.is_reservation_key(reservation_key_value)
        and _metadata_matches_reservation_key(str(reservation_key_value), metadata)
        and metadata.get("job_id") == record.get("job_id")
        and metadata.get("attempt_id") == record.get("active_attempt_id")
        and metadata.get("server_instance_id") == record.get("server_instance_id")
    )


def _read_outcome_record(job_record: dict) -> dict | None:
    job_id = job_record.get("job_id")
    attempt_id = job_record.get("active_attempt_id")
    if not reservations.is_job_id(job_id) or not reservations.is_attempt_id(attempt_id):
        return None
    try:
        outcome = reservations.read_json_file_no_follow(
            reservations.outcome_record_path(REPO_ROOT, job_id, attempt_id)
        )
    except Exception:
        return None
    if not isinstance(outcome, dict):
        return None
    if outcome.get("job_id") != job_id or outcome.get("attempt_id") != attempt_id:
        return None
    if outcome.get("schema_version") != 1:
        return None
    if outcome.get("reservation_key") != job_record.get("reservation_key"):
        return None
    if outcome.get("host") != job_record.get("host"):
        return None
    if outcome.get("gpu_index") != job_record.get("gpu_index"):
        return None
    if (
        job_record.get("remote_pid") is not None
        and outcome.get("remote_pid") not in {None, job_record.get("remote_pid")}
    ):
        return None
    if outcome.get("terminal_status") not in {"success", "failure", "signaled", "launcher_error"}:
        return None
    return outcome


def _job_record_from_reservation_key(
    reservation_key_value: str,
    *,
    metadata: dict | None = None,
) -> dict | None:
    jobs_root = reservations.repo_job_state_root(REPO_ROOT)
    if not jobs_root.exists():
        return None
    expected_job_id = None if metadata is None else metadata.get("job_id")
    expected_attempt_id = None if metadata is None else metadata.get("attempt_id")
    expected_server_id = None if metadata is None else metadata.get("server_instance_id")
    for job_file in sorted(jobs_root.glob("job-*/job.json")):
        try:
            record = reservations.read_json_file_no_follow(job_file)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("reservation_key") != reservation_key_value:
            continue
        if expected_job_id is not None and record.get("job_id") != expected_job_id:
            continue
        if expected_attempt_id is not None and record.get("active_attempt_id") != expected_attempt_id:
            continue
        if expected_server_id is not None and record.get("server_instance_id") != expected_server_id:
            continue
        if record.get("reservation_key") == reservation_key_value:
            return record
    return None


def _repo_reservation_candidates() -> list[dict]:
    registry_status, _registry_error, rows = _load_reservation_rows(scope="mine")
    if registry_status != "ok":
        return []
    return [rows[key] for key in sorted(rows)]


def _resolve_job_target(job_id: str | None, reservation_key_value: str | None) -> tuple[dict | None, str, list[dict]]:
    if job_id is not None:
        record = _read_repo_job_record(job_id)
        if record is not None and reservations.is_reservation_key(record.get("reservation_key")):
            metadata = _read_reservation_metadata(str(record["reservation_key"]))
            if not _job_record_matches_current_metadata(record, metadata):
                record = dict(record)
                record["active_reservation"] = False
                record["reservation_identity_mismatch"] = True
        return record, "job_id", []
    if reservation_key_value is not None:
        metadata = _read_reservation_metadata(reservation_key_value)
        record = _job_record_from_reservation_key(reservation_key_value, metadata=metadata)
        if record is not None:
            return record, "reservation_key", []
        if metadata is not None:
            return {
                "schema_version": 1,
                "repo_local_record": False,
                "job_id": metadata.get("job_id"),
                "active_attempt_id": metadata.get("attempt_id"),
                "reservation_key": reservation_key_value,
                "host": metadata.get("host"),
                "gpu_index": metadata.get("gpu_index"),
                "script_name": metadata.get("script_name"),
                "output_file": None,
                "next_poll_after": None,
                "remote_pid": metadata.get("remote_pid"),
                "process_fingerprint": metadata.get("process_fingerprint"),
            }, "reservation_key", []
        return None, "reservation_key", []
    candidates = _repo_reservation_candidates()
    if len(candidates) == 1:
        metadata = _read_reservation_metadata(str(candidates[0]["reservation_key"]))
        record = _job_record_from_reservation_key(str(candidates[0]["reservation_key"]), metadata=metadata)
        if record is not None:
            return record, "single_repo_reservation", []
        if metadata is not None:
            return {
                "schema_version": 1,
                "repo_local_record": False,
                "job_id": metadata.get("job_id"),
                "active_attempt_id": metadata.get("attempt_id"),
                "reservation_key": metadata.get("reservation_key"),
                "host": metadata.get("host"),
                "gpu_index": metadata.get("gpu_index"),
                "script_name": metadata.get("script_name"),
                "output_file": None,
                "next_poll_after": None,
                "remote_pid": metadata.get("remote_pid"),
                "process_fingerprint": metadata.get("process_fingerprint"),
            }, "single_repo_reservation", []
    if len(candidates) > 1:
        return None, "ambiguous", candidates
    return None, "none", []


def _read_current_job_metadata(job_record: dict) -> tuple[dict | None, str]:
    reservation_key_value = job_record.get("reservation_key")
    if not reservations.is_reservation_key(reservation_key_value):
        return None, "job record has invalid reservation_key"
    metadata = _read_reservation_metadata(str(reservation_key_value))
    if metadata is None:
        return None, "reservation metadata is unavailable"
    if not _metadata_matches_reservation_key(str(reservation_key_value), metadata):
        return None, "reservation metadata does not match reservation_key"
    if metadata.get("job_id") != job_record.get("job_id"):
        return None, "reservation metadata belongs to a different job"
    if metadata.get("attempt_id") != job_record.get("active_attempt_id"):
        return None, "reservation metadata belongs to a different attempt"
    return metadata, ""


def _owned_lifecycle_metadata(job_record: dict) -> tuple[dict | None, str]:
    metadata, reason = _read_current_job_metadata(job_record)
    if metadata is None:
        return None, reason
    reservation_key_value = str(metadata["reservation_key"])
    if metadata.get("server_instance_id") != SERVER_INSTANCE_ID:
        return None, "only the owner server that owns this reservation may change the job"
    if reservation_key_value not in HEARTBEAT_MANAGER.owned_keys():
        return None, "current server is not heartbeating this reservation"
    return metadata, ""


def _write_repo_job_record(job_record: dict) -> None:
    job_id = job_record.get("job_id")
    if not reservations.is_job_id(job_id):
        raise ValueError("job record has invalid job_id")
    reservations.atomic_write_json(
        reservations.job_record_path(REPO_ROOT, str(job_id)),
        job_record,
    )


def _signal_managed_process(metadata: dict, *, signal_name: str = "TERM") -> tuple[bool, str]:
    pid = metadata.get("remote_pid")
    host = metadata.get("host")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False, "reservation has no positive remote_pid to signal"
    if not isinstance(host, str) or not _is_allowed_host(host):
        return False, "reservation host is not signalable under active policy"
    try:
        normalized, signal_number = _normalize_kill_signal(signal_name)
    except ValueError as exc:
        return False, str(exc)
    result = _host_run(host, f"kill -{signal_number} {pid}", timeout=2)
    if result is None:
        return False, _host_run_error(host, f"kill -{signal_number} {pid}") or "signal command failed"
    return True, normalized


def _owned_lifecycle_refusal(action: str, job_record: dict, reason: str, *, process: dict | None = None) -> str:
    reservation_key_value = job_record.get("reservation_key")
    owned = (
        reservations.is_reservation_key(reservation_key_value)
        and str(reservation_key_value) in HEARTBEAT_MANAGER.owned_keys()
    )
    healthy = HEARTBEAT_MANAGER.is_healthy()
    return _json_tool_response({
        "status": "refused",
        "action": action,
        "reason": reason,
        "job_id": job_record.get("job_id"),
        "attempt_id": job_record.get("active_attempt_id"),
        "reservation_key": job_record.get("reservation_key"),
        "owned_by_current_server": owned,
        "heartbeat_manager_healthy": healthy,
        "heartbeat_manager_reason": HEARTBEAT_MANAGER.health_reason(),
        "process": process,
        "allowed_actions": ["status", "stop", "retry", "finish"] if owned and healthy else ["status"],
        "server_instance_id": SERVER_INSTANCE_ID,
    })


def _outcome_base(
    *,
    job_id: str,
    attempt_id: str,
    reservation_key_value: str,
    host: str,
    gpu_index: int,
) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": host,
        "gpu_index": gpu_index,
        "remote_pid": None,
        "terminal_status": "launcher_error",
        "exit_code": None,
        "signal": None,
        "started_at": None,
        "ended_at": None,
        "error_summary": None,
    }


def _metadata_still_matches_retry_source(latest: dict, source: dict) -> bool:
    return (
        latest.get("job_id") == source.get("job_id")
        and latest.get("attempt_id") == source.get("attempt_id")
        and latest.get("reservation_key") == source.get("reservation_key")
        and latest.get("server_instance_id") == source.get("server_instance_id")
        and latest.get("remote_pid") == source.get("remote_pid")
        and _metadata_matches_reservation_key(str(source.get("reservation_key")), latest)
    )


def _metadata_path_for_key(registry_root: Path, reservation_key_value: str) -> Path:
    return reservations.reservation_dir(registry_root, reservation_key_value) / "metadata.json"


def _claim_retry_attempt(
    *,
    source_record: dict,
    source_metadata: dict,
    pending_record: dict,
    attempt_id: str,
) -> tuple[bool, str]:
    reservation_key_value = str(source_metadata["reservation_key"])
    registry_root = reservations.reservation_registry_root()
    metadata_path = _metadata_path_for_key(registry_root, reservation_key_value)
    with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
        latest = _read_reservation_metadata(reservation_key_value)
        if latest is None:
            return False, "reservation disappeared before retry launch"
        if not _metadata_still_matches_retry_source(latest, source_metadata):
            return False, "reservation changed before retry launch; retry was not started"
        updated_metadata = dict(latest)
        updated_metadata.update({
            "attempt_id": attempt_id,
            "remote_pid": None,
            "process_fingerprint": None,
            "last_heartbeat_at": reservations.iso_timestamp(),
        })
        try:
            _write_repo_job_record(pending_record)
            reservations.atomic_write_json(metadata_path, updated_metadata)
        except Exception as exc:
            try:
                _write_repo_job_record(source_record)
            except Exception:
                pass
            return False, f"failed to claim retry attempt: {exc}"
    return True, ""


def _rollback_retry_claim(
    *,
    source_record: dict,
    source_metadata: dict,
    attempt_id: str,
) -> None:
    reservation_key_value = str(source_metadata["reservation_key"])
    registry_root = reservations.reservation_registry_root()
    metadata_path = _metadata_path_for_key(registry_root, reservation_key_value)
    try:
        with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
            latest = _read_reservation_metadata(reservation_key_value)
            if latest is None:
                return
            if (
                latest.get("job_id") != source_metadata.get("job_id")
                or latest.get("attempt_id") != attempt_id
                or latest.get("server_instance_id") != source_metadata.get("server_instance_id")
                or latest.get("remote_pid") is not None
            ):
                return
            _write_repo_job_record(source_record)
            restored = dict(source_metadata)
            restored["last_heartbeat_at"] = reservations.iso_timestamp()
            reservations.atomic_write_json(metadata_path, restored)
    except Exception:
        return


def _finalize_retry_attempt(
    *,
    pending_record: dict,
    source_metadata: dict,
    attempt_id: str,
    remote_pid: int,
    process_fingerprint: str,
    remote_start_time: str | None,
    launch_mode: str,
) -> tuple[bool, str]:
    reservation_key_value = str(source_metadata["reservation_key"])
    registry_root = reservations.reservation_registry_root()
    metadata_path = _metadata_path_for_key(registry_root, reservation_key_value)
    with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
        latest = _read_reservation_metadata(reservation_key_value)
        if latest is None:
            return False, "reservation disappeared after retry launch"
        if (
            latest.get("job_id") != source_metadata.get("job_id")
            or latest.get("attempt_id") != attempt_id
            or latest.get("server_instance_id") != source_metadata.get("server_instance_id")
            or latest.get("remote_pid") is not None
            or not _metadata_matches_reservation_key(reservation_key_value, latest)
        ):
            return False, "reservation changed after retry launch"
        launched_record = dict(pending_record)
        launched_record.update({
            "remote_pid": remote_pid,
            "remote_start_time": remote_start_time,
            "process_fingerprint": process_fingerprint,
            "launch_mode": launch_mode,
        })
        latest.update({
            "remote_pid": remote_pid,
            "remote_start_time": remote_start_time,
            "process_fingerprint": process_fingerprint,
            "last_heartbeat_at": reservations.iso_timestamp(),
        })
        _write_repo_job_record(launched_record)
        reservations.atomic_write_json(metadata_path, latest)
    return True, ""


def _read_retry_pid_ack(pid_ack_path: Path, *, attempts: int = 10) -> int | None:
    for _ in range(attempts):
        try:
            ack = reservations.read_json_file_no_follow(pid_ack_path)
        except Exception:
            time.sleep(0.05)
            continue
        if isinstance(ack, dict):
            pid = ack.get("remote_pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                return pid
        return None
    return None


def _positive_pid_from_text(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    for line in reversed(value.strip().splitlines()):
        text = line.strip()
        if text.isdigit() and int(text) > 0:
            return int(text)
    return None


def _start_managed_attempt(
    *,
    job_record: dict,
    metadata: dict,
    attempt_id: str,
    output_path: Path,
    next_poll_after: str,
    async_mode_requested: bool,
) -> str:
    job_id = str(job_record["job_id"])
    reservation_key_value = str(metadata["reservation_key"])
    host = str(metadata["host"])
    gpu_index = int(metadata["gpu_index"])
    argv = _build_python_gpu_argv(
        str(job_record["script_path"]),
        args=[str(arg) for arg in job_record.get("args", [])],
        execution_host=host,
    )
    job_env = _gpu_job_env(gpu_index)
    process_nonce = secrets.token_urlsafe(18)
    process_fingerprint = _managed_process_fingerprint(job_id, attempt_id, host, process_nonce)
    job_env["GPU_MCP_PROCESS_FINGERPRINT"] = process_fingerprint
    outcome_path = reservations.outcome_record_path(REPO_ROOT, job_id, attempt_id)
    outcome_base = _outcome_base(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key_value,
        host=host,
        gpu_index=gpu_index,
    )

    pending_record = dict(job_record)
    pending_record.update({
        "active_attempt_id": attempt_id,
        "next_poll_after": next_poll_after,
        "last_status_checked_at": None,
        "remote_pid": None,
        "process_fingerprint": None,
        "outcome_file": str(outcome_path),
        "retry_started_at": reservations.iso_timestamp(),
    })
    claimed, claim_reason = _claim_retry_attempt(
        source_record=job_record,
        source_metadata=metadata,
        pending_record=pending_record,
        attempt_id=attempt_id,
    )
    if not claimed:
        return _launch_handle_response(
            status="refused",
            reason=claim_reason,
            job_id=job_id,
            attempt_id=attempt_id,
            reservation_key_value=reservation_key_value,
            host=host,
            gpu_index=gpu_index,
            output_path=output_path,
            next_poll_after=next_poll_after,
            job_lifecycle="retry_not_started",
            launch="none",
            async_mode_requested=async_mode_requested,
        )

    if _is_local_host(host):
        try:
            env = os.environ.copy()
            env.update(job_env)
            supervisor_argv = _managed_supervisor_argv(
                argv,
                output_path,
                job_env,
                outcome_path,
                outcome_base,
            )
            proc = subprocess.Popen(
                supervisor_argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(REPO_ROOT),
                start_new_session=True,
            )
        except Exception as exc:
            _rollback_retry_claim(
                source_record=job_record,
                source_metadata=metadata,
                attempt_id=attempt_id,
            )
            return _launch_handle_response(
                status="refused",
                reason=f"Failed to relaunch local GPU job on {host}: {exc}",
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key_value,
                host=host,
                gpu_index=gpu_index,
                output_path=output_path,
                next_poll_after=next_poll_after,
                job_lifecycle="retry_launch_failed",
                launch="local",
                async_mode_requested=async_mode_requested,
            )
        pid_value = str(proc.pid)
        launch_mode = "local"
        remote_start_time = _process_start_time(host, int(pid_value))
    else:
        pid_ack_path = outcome_path.parent / "launcher_pid.json"
        launch_cmd = _remote_async_launch_command(
            argv,
            output_path,
            job_env,
            outcome_path,
            outcome_base,
            pid_ack_path=pid_ack_path,
        )
        bg_cmd = _remote_repo_command(launch_cmd)
        pid_str = _ssh_run(host, bg_cmd)
        acknowledged_pid = _positive_pid_from_text(pid_str)
        if acknowledged_pid is None:
            acknowledged_pid = _read_retry_pid_ack(pid_ack_path)
        if acknowledged_pid is None:
            if pid_ack_path.exists():
                return _launch_handle_response(
                    status="launch_outcome_unknown",
                    reason=(
                        f"SSH retry did not return a usable PID from {host}, "
                        "but the launcher acknowledgement file exists; reservation kept fail-closed"
                    ),
                    job_id=job_id,
                    attempt_id=attempt_id,
                    reservation_key_value=reservation_key_value,
                    host=host,
                    gpu_index=gpu_index,
                    output_path=output_path,
                    next_poll_after=next_poll_after,
                    job_lifecycle="launch_outcome_unknown",
                    launch="ssh",
                    async_mode_requested=async_mode_requested,
                )
            return _launch_handle_response(
                status="launch_outcome_unknown",
                reason=(
                    f"SSH retry did not return a PID acknowledgement from {host}; "
                    "reservation kept fail-closed because the retry launch outcome is unknown"
                ),
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key_value,
                host=host,
                gpu_index=gpu_index,
                output_path=output_path,
                next_poll_after=next_poll_after,
                job_lifecycle="launch_outcome_unknown",
                launch="ssh",
                async_mode_requested=async_mode_requested,
            )
        pid_value = str(acknowledged_pid)
        launch_mode = "ssh"
        remote_start_time = None

    finalized, finalize_reason = _finalize_retry_attempt(
        pending_record=pending_record,
        source_metadata=metadata,
        attempt_id=attempt_id,
        remote_pid=int(pid_value),
        process_fingerprint=process_fingerprint,
        remote_start_time=remote_start_time,
        launch_mode=launch_mode,
    )
    if not finalized:
        return _launch_handle_response(
            status="launched_with_warning",
            reason=finalize_reason,
            job_id=job_id,
            attempt_id=attempt_id,
            reservation_key_value=reservation_key_value,
            host=host,
            gpu_index=gpu_index,
            output_path=output_path,
            next_poll_after=next_poll_after,
            job_lifecycle="running",
            launch=launch_mode,
            async_mode_requested=async_mode_requested,
            process={
                "remote_pid": int(pid_value),
                "remote_start_time": remote_start_time,
                "remote_boot_id": None,
                "process_fingerprint": process_fingerprint,
            },
        )
    return _launch_handle_response(
        status="retried",
        reason="",
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key_value,
        host=host,
        gpu_index=gpu_index,
        output_path=output_path,
        next_poll_after=next_poll_after,
        job_lifecycle="running",
        launch=launch_mode,
        async_mode_requested=async_mode_requested,
        process={
            "remote_pid": int(pid_value),
            "remote_start_time": remote_start_time,
            "remote_boot_id": None,
            "process_fingerprint": process_fingerprint,
        },
    )


def _remove_owned_finished_reservation(job_record: dict, metadata: dict) -> tuple[bool, str]:
    reservation_key_value = str(metadata["reservation_key"])
    registry_root = reservations.reservation_registry_root()
    with reservations.cleanup_finalization_guard(registry_root, reservation_key_value):
        latest = _read_reservation_metadata(reservation_key_value)
        if latest is None:
            return False, "reservation disappeared during finish"
        if latest.get("server_instance_id") != SERVER_INSTANCE_ID:
            return False, "reservation owner changed during finish"
        if (
            latest.get("job_id") != job_record.get("job_id")
            or latest.get("attempt_id") != job_record.get("active_attempt_id")
            or latest.get("remote_pid") != metadata.get("remote_pid")
            or not _metadata_matches_reservation_key(reservation_key_value, latest)
        ):
            return False, "reservation metadata changed during finish"
        reservations.remove_reservation_if_launch_failed(
            registry_root=registry_root,
            reservation_key_value=reservation_key_value,
        )
        return True, ""


def _stop_owned_job(action: str, job_record: dict) -> str:
    metadata, reason = _owned_lifecycle_metadata(job_record)
    if metadata is None:
        return _owned_lifecycle_refusal(action, job_record, reason)
    inspection = _inspect_reservation_process(metadata)
    if inspection["status"] == "unknown":
        return _owned_lifecycle_refusal(
            action,
            job_record,
            "cannot prove the original process identity; stop refused fail-closed",
            process=inspection,
        )
    if inspection["status"] == "gone":
        response = json.loads(_job_status_response(action="status", job_record=job_record))
        response.update({
            "status": "stop_not_needed",
            "action": "stop",
            "reason": "matching process is already gone; reservation remains until retry or finish",
            "process": inspection,
        })
        return _json_tool_response(response)
    ok, signal_or_reason = _signal_managed_process(metadata, signal_name="TERM")
    if not ok:
        return _owned_lifecycle_refusal(
            action,
            job_record,
            f"failed to signal managed process: {signal_or_reason}",
            process=inspection,
        )
    now = reservations.iso_timestamp()
    updated = dict(job_record)
    updated["stop_requested_at"] = now
    _write_repo_job_record(updated)
    HEARTBEAT_MANAGER.update_owned_metadata(str(metadata["reservation_key"]), {
        "stop_requested_at": now,
        "last_heartbeat_at": now,
    })
    response = json.loads(_job_status_response(action="status", job_record=updated))
    response.update({
        "status": "stop_requested",
        "action": "stop",
        "reason": "TERM sent to the managed launcher; reservation remains held until retry or finish",
        "job_lifecycle": "stopping",
        "process": dict(inspection, signal_sent=signal_or_reason),
    })
    return _json_tool_response(response)


def _retry_owned_job(job_record: dict) -> str:
    metadata, reason = _owned_lifecycle_metadata(job_record)
    if metadata is None:
        return _owned_lifecycle_refusal("retry", job_record, reason)
    inspection = _inspect_reservation_process(metadata)
    if inspection["status"] == "alive":
        return _owned_lifecycle_refusal(
            "retry",
            job_record,
            (
                "retry would create a second process on the same reserved GPU; "
                "keep checking status or explicitly stop the live attempt first"
            ),
            process=inspection,
        )
    if inspection["status"] == "unknown":
        return _owned_lifecycle_refusal(
            "retry",
            job_record,
            (
                "retry would create a second process on the same reserved GPU unless "
                "the previous attempt is proven gone; cannot prove the previous "
                "attempt is gone, so retry is refused fail-closed"
            ),
            process=inspection,
        )
    output_file = job_record.get("output_file")
    try:
        out_path = _validate_output_path(None if output_file is None else str(output_file))
        _prepare_output_parent(out_path)
    except ValueError as exc:
        return _owned_lifecycle_refusal("retry", job_record, str(exc), process=inspection)
    attempt_id = reservations.generate_attempt_id()
    next_poll_after = _next_poll_after(reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC)
    return _start_managed_attempt(
        job_record=job_record,
        metadata=metadata,
        attempt_id=attempt_id,
        output_path=out_path,
        next_poll_after=next_poll_after,
        async_mode_requested=False,
    )


def _finish_owned_job(job_record: dict) -> str:
    metadata, reason = _owned_lifecycle_metadata(job_record)
    if metadata is None:
        return _owned_lifecycle_refusal("finish", job_record, reason)
    reservation_key_value = str(metadata["reservation_key"])
    inspection = _inspect_reservation_process(metadata)
    if inspection["status"] == "gone":
        HEARTBEAT_MANAGER.unregister(reservation_key_value)
        removed, remove_reason = _remove_owned_finished_reservation(job_record, metadata)
        response = json.loads(_job_status_response(action="status", job_record=job_record))
        response.update({
            "status": "finished" if removed else "refused",
            "action": "finish",
            "reason": remove_reason or "reservation removed after process-gone proof",
            "reservation_state": None if removed else response.get("reservation_state"),
            "job_lifecycle": "finished" if removed else response.get("job_lifecycle"),
            "owned_by_current_server": False,
            "allowed_actions": ["status"],
            "process": inspection,
            "next_poll_after": None if removed else response.get("next_poll_after"),
        })
        return _json_tool_response(response)
    if inspection["status"] == "alive":
        return _owned_lifecycle_refusal(
            "finish",
            job_record,
            (
                "finish refused because the matching process is still alive; call "
                "stop first if you intend to terminate this job, or keep polling "
                "status if you intend to wait; call finish only after status proves "
                "the process is gone"
            ),
            process=inspection,
        )
    return _owned_lifecycle_refusal(
        "finish",
        job_record,
        (
            "finish refused because the matching process is not proven gone; keep "
            "polling status or call stop first if you intend to terminate this job, "
            "then call finish only after the process is gone"
        ),
        process=inspection,
    )


def _update_owned_job_cadence(
    job_record: dict,
    *,
    expected_duration_sec: int | None,
    cadence_hint_sec: int | None,
    reason: str | None,
) -> str:
    reason_text = _nonempty_text(reason)
    if reason_text is None:
        return _json_tool_response({
            "status": "refused",
            "action": "update_cadence",
            "reason": "update_cadence requires a non-empty reason",
            "job_id": job_record.get("job_id"),
            "reservation_key": job_record.get("reservation_key"),
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if expected_duration_sec is None and cadence_hint_sec is None:
        return _json_tool_response({
            "status": "refused",
            "action": "update_cadence",
            "reason": "update_cadence requires expected_duration_sec or cadence_hint_sec",
            "job_id": job_record.get("job_id"),
            "reservation_key": job_record.get("reservation_key"),
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    expected_duration, duration_error = _validate_positive_int(
        expected_duration_sec,
        name="expected_duration_sec",
    )
    if duration_error:
        return _json_tool_response({
            "status": "refused",
            "action": "update_cadence",
            "reason": duration_error,
            "job_id": job_record.get("job_id"),
            "reservation_key": job_record.get("reservation_key"),
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    cadence_hint, cadence_error = _validate_positive_int(cadence_hint_sec, name="cadence_hint_sec")
    if cadence_error:
        return _json_tool_response({
            "status": "refused",
            "action": "update_cadence",
            "reason": cadence_error,
            "job_id": job_record.get("job_id"),
            "reservation_key": job_record.get("reservation_key"),
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    metadata, metadata_reason = _owned_lifecycle_metadata(job_record)
    if metadata is None:
        return _owned_lifecycle_refusal("update_cadence", job_record, metadata_reason)
    interval_sec, cadence_basis, _positive_smoke = _phase7_cadence_basis(
        job_role=str(job_record.get("job_role") or "one_off"),
        expected_duration_sec=expected_duration,
        cadence_hint_sec=cadence_hint,
        smoke_job_id=job_record.get("smoke_job_id"),
        smoke_cadence_representative=bool(job_record.get("smoke_cadence_representative")),
        smoke_skip_reason=_nonempty_text(job_record.get("smoke_skip_reason")),
    )
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_text = reservations.iso_timestamp(now)
    next_poll_after = _next_poll_after_from(now, interval_sec)
    updated = dict(job_record)
    updated.update({
        "heartbeat_interval_sec": interval_sec,
        "cadence_basis": cadence_basis,
        "cadence_update_reason": reason_text,
        "last_heartbeat_at": now_text,
        "next_poll_after": next_poll_after,
    })
    HEARTBEAT_MANAGER.update_owned_metadata(str(metadata["reservation_key"]), {
        "heartbeat_interval_sec": interval_sec,
        "last_heartbeat_at": now_text,
    })
    _write_repo_job_record(updated)
    return _json_tool_response({
        "status": "ok",
        "action": "update_cadence",
        "reason": reason_text,
        "job_id": updated.get("job_id"),
        "attempt_id": updated.get("active_attempt_id"),
        "reservation_key": updated.get("reservation_key"),
        "heartbeat_interval_sec": interval_sec,
        "cadence_basis": cadence_basis,
        "next_poll_after": next_poll_after,
        "server_instance_id": SERVER_INSTANCE_ID,
    })


def _job_heartbeat_interval(job_record: dict) -> int:
    raw = job_record.get("heartbeat_interval_sec")
    if isinstance(raw, bool):
        return reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC
    try:
        return _clamp_cadence_interval(int(raw))
    except (TypeError, ValueError):
        return reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC


def _compact_not_due_response(*, action: str, job_record: dict, due_at: datetime) -> str:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    seconds_until_due = max(0, int((due_at - now).total_seconds()))
    return _json_tool_response({
        "status": "ok",
        "action": action,
        "reason": "status check is earlier than next_poll_after",
        "polling_state": "not_due_yet",
        "full_status_performed": False,
        "remote_inspection_performed": False,
        "log_tail_included": False,
        "job_id": job_record.get("job_id"),
        "attempt_id": job_record.get("active_attempt_id"),
        "reservation_key": job_record.get("reservation_key"),
        "job_role": job_record.get("job_role"),
        "job_lifecycle": "running",
        "heartbeat_interval_sec": _job_heartbeat_interval(job_record),
        "next_poll_after": job_record.get("next_poll_after"),
        "seconds_until_due": seconds_until_due,
        "output": {
            "path": job_record.get("output_file"),
        },
        "agent_guidance": (
            "This job is not due yet. Do independent work or wait until next_poll_after; "
            "retry with early_poll_reason for an immediate full check."
        ),
        "server_instance_id": SERVER_INSTANCE_ID,
    })


def _smoke_status_recommendation(job_record: dict, job_lifecycle: str) -> dict | None:
    if job_record.get("job_role") != "smoke" or job_lifecycle != "succeeded":
        return None
    return {
        "tool": "run_python_on_gpu",
        "kwargs": {
            "job_role": "main",
            "smoke_job_id": job_record.get("job_id"),
            "smoke_cadence_representative": False,
        },
    }


def _job_status_response(
    *,
    action: str,
    job_record: dict,
    reason: str = "",
    early_poll_reason: str | None = None,
    allow_compact_early: bool = False,
) -> str:
    early_reason = _nonempty_text(early_poll_reason)
    local_outcome = _read_outcome_record(job_record)
    if (
        action == "status"
        and allow_compact_early
        and early_reason is None
        and local_outcome is None
        and job_record.get("active_reservation", True) is not False
        and job_record.get("repo_local_record", True)
        and job_record.get("phase7_poll_discipline") is True
        and reservations.is_job_id(job_record.get("job_id"))
    ):
        due_at = _parse_next_poll_after(job_record.get("next_poll_after"))
        if due_at is not None and due_at > datetime.now(timezone.utc):
            return _compact_not_due_response(action=action, job_record=job_record, due_at=due_at)

    reservation_key_value = job_record.get("reservation_key")
    reservation = None
    active_reservation = job_record.get("active_reservation", True) is not False
    if active_reservation and reservations.is_reservation_key(reservation_key_value):
        _, _, rows = _load_reservation_rows(scope="all")
        reservation = rows.get(str(reservation_key_value))
    owned = bool(reservation and reservation.get("owned_by_current_server"))
    healthy = HEARTBEAT_MANAGER.is_healthy()
    allowed_actions = (
        ["status", "stop", "retry", "finish", "update_cadence"]
        if owned and healthy
        else ["status"]
    )
    process = {
        "remote_pid": job_record.get("remote_pid"),
        "process_fingerprint": job_record.get("process_fingerprint"),
    }
    remote_inspection_performed = False
    job_lifecycle = "running"
    outcome = local_outcome
    if not active_reservation:
        if outcome is None:
            job_lifecycle = "process_gone_unknown_outcome"
        else:
            terminal = outcome["terminal_status"]
            job_lifecycle = "succeeded" if terminal == "success" else "failed"
    if reservation is not None:
        metadata = _read_reservation_metadata(str(reservation_key_value))
        if metadata is not None and isinstance(metadata.get("remote_pid"), int):
            inspection = _inspect_reservation_process(metadata)
            remote_inspection_performed = True
            process.update(inspection)
            if inspection["status"] == "gone":
                outcome = _read_outcome_record(job_record)
                if outcome is None:
                    job_lifecycle = "process_gone_unknown_outcome"
                else:
                    terminal = outcome["terminal_status"]
                    job_lifecycle = "succeeded" if terminal == "success" else "failed"
    return_next_poll_after = job_record.get("next_poll_after")
    if (
        job_lifecycle == "running"
        and active_reservation
        and job_record.get("repo_local_record", True)
        and reservations.is_job_id(job_record.get("job_id"))
    ):
        return_next_poll_after = _next_poll_after(_job_heartbeat_interval(job_record))
        updated = dict(job_record)
        updated["next_poll_after"] = return_next_poll_after
        updated["last_status_checked_at"] = reservations.iso_timestamp()
        if early_reason is not None:
            updated["last_early_poll_reason"] = early_reason
            updated["last_early_poll_at"] = updated["last_status_checked_at"]
        try:
            reservations.atomic_write_json(
                reservations.job_record_path(REPO_ROOT, str(job_record["job_id"])),
                updated,
            )
        except Exception:
            pass
    response = {
        "status": "ok" if action == "status" else "refused",
        "action": action,
        "reason": reason,
        "polling_state": "full_status",
        "full_status_performed": True,
        "remote_inspection_performed": remote_inspection_performed,
        "log_tail_included": False,
        "early_poll_override_recorded": bool(early_reason),
        "job_id": job_record.get("job_id"),
        "attempt_id": job_record.get("active_attempt_id"),
        "reservation_key": reservation_key_value,
        "active_reservation": active_reservation,
        "reservation_identity_mismatch": bool(job_record.get("reservation_identity_mismatch")),
        "reservation_state": None if reservation is None else reservation.get("reservation_state"),
        "job_lifecycle": job_lifecycle,
        "outcome": outcome,
        "owned_by_current_server": owned,
        "heartbeat": None if reservation is None else reservation.get("heartbeat"),
        "heartbeat_manager_healthy": healthy,
        "heartbeat_manager_reason": HEARTBEAT_MANAGER.health_reason(),
        "process": process,
        "allowed_actions": allowed_actions,
        "job_role": job_record.get("job_role"),
        "job_role_defaulted": job_record.get("job_role_defaulted"),
        "heartbeat_interval_sec": _job_heartbeat_interval(job_record),
        "cadence_basis": job_record.get("cadence_basis"),
        "output": {
            "path": job_record.get("output_file"),
        },
        "next_poll_after": None if job_lifecycle in {"succeeded", "failed"} else return_next_poll_after,
        "agent_guidance": (
            "Do not use output-dependent results until status is terminal; independent work may continue."
            if job_lifecycle == "running"
            else "Terminal status reached; outputs may be inspected."
        ),
        "server_instance_id": SERVER_INSTANCE_ID,
    }
    recommendation = _smoke_status_recommendation(job_record, job_lifecycle)
    if recommendation is not None:
        response["recommended_next_call"] = recommendation
    return _json_tool_response(response)


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
    """Check GPU availability across the cluster with reservation overlay.

    Args:
        samples: Number of utilization samples to average (default 2).
        threshold: GPU utilization % at or below which a GPU is marked AVAILABLE (default 10).

    Returns:
        JSON report with per-GPU availability and registry status.
    """
    _apply_test_controls_from_file()
    if stale := _stale_policy_refusal():
        return stale
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 1:
        return _json_tool_response({"status": "refused", "reason": "samples must be a positive integer"})
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        return _json_tool_response({"status": "refused", "reason": "threshold must be a non-negative integer"})
    registry_status, registry_error, reservation_rows = _load_reservation_rows(scope="all")
    if registry_status == "ok":
        reservation_rows = _refresh_stale_reservations(reservation_rows)
    query = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits"
    )
    gpu_rows: list[dict] = []
    human_lines = [f"==> Starting GPU check: {samples} samples, threshold <= {threshold}%"]
    remote_successes = 0

    for node in NODES:
        user, host = _node_user_host(node)
        canonical_host = reservations.canonical_host_component(host)
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

        human_lines.append(f"[{short_host} {route}]")
        if failed or not sample_sets:
            reason = _host_run_error("local" if local_host else host, query)
            suffix = f": {reason}" if reason else ""
            human_lines.append(f"  ssh/nvidia-smi failed{suffix}")
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
            key = reservations.reservation_key(canonical_host, int(last["index"]))
            reservation = reservation_rows.get(key)
            nvsmi_available = (
                avg_util is not None
                and avg_util <= threshold
                and (mem_free is None or mem_free >= CONFIG_POLICY.min_free_memory_mib)
            )
            if registry_status != "ok":
                availability = "unknown_unavailable"
                reservation_state = None
            elif reservation is not None:
                availability = "reserved"
                reservation_state = reservation.get("reservation_state") or "RESERVED"
            else:
                availability = "available" if nvsmi_available else "busy"
                reservation_state = None
            mem_text = (
                f"{last['memory_used_MiB']}/{last['memory_total_MiB']} MiB"
                if last["memory_total_MiB"] is not None
                else f"{last['memory_used_MiB']} MiB"
            )
            util_text = f"{avg_util:.1f}%" if avg_util is not None else "N/A"
            human_lines.append(
                f"  GPU {last['index']} | {last['name']} | util_avg={util_text} | "
                f"mem={mem_text} | {availability.upper()}"
            )
            gpu_rows.append({
                "host": canonical_host,
                "gpu_index": int(last["index"]),
                "name": last["name"],
                "utilization_avg_pct": avg_util,
                "memory_used_MiB": last["memory_used_MiB"],
                "memory_total_MiB": last["memory_total_MiB"],
                "memory_free_MiB": mem_free,
                "availability": availability,
                "reservation_key": key,
                "reservation_state": reservation_state,
                "reservation": reservation,
            })

    if remote_successes == 0:
        human_lines.append(
            "WARNING: no non-local SSH GPU host succeeded; remote MCP SSH "
            "routing is not installed from this control host."
        )

    return _json_tool_response({
        "status": "ok" if registry_status == "ok" else "error",
        "registry_status": registry_status,
        "registry_error": registry_error,
        "samples": samples,
        "threshold": threshold,
        "gpus": gpu_rows,
        "human_report": "\n".join(human_lines) if len(human_lines) > 1 else "(no configured GPU nodes available)",
    })


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
    _apply_test_controls_from_file()
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


def _manage_gpu_job_impl(
    action: str = "status",
    job_id: Optional[str] = None,
    reservation_key: Optional[str] = None,
    expected_duration_sec: Optional[int] = None,
    cadence_hint_sec: Optional[int] = None,
    reason: Optional[str] = None,
    early_poll_reason: Optional[str] = None,
):
    """Inspect or change a managed GPU job.

    Phase 0 exposes the stable JSON contract and target-validation surface.
    Reservation acquisition, status recovery, stop, retry, and finish behavior
    are implemented in later ADR 0004 phases.
    """
    _apply_test_controls_from_file()
    if stale := _stale_policy_refusal():
        return stale
    if not isinstance(action, str):
        return _json_tool_response({
            "status": "refused",
            "reason": "action must be a string",
            "allowed_actions": ["status", "stop", "retry", "finish", "update_cadence"],
        })
    normalized_action = action.strip().lower()
    if normalized_action not in {"status", "stop", "retry", "finish", "update_cadence"}:
        return _json_tool_response({
            "status": "refused",
            "action": normalized_action,
            "reason": "action must be one of: status, stop, retry, finish, update_cadence",
            "allowed_actions": ["status", "stop", "retry", "finish", "update_cadence"],
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if job_id is not None and not reservations.is_job_id(job_id):
        return _json_tool_response({
            "status": "refused",
            "action": normalized_action,
            "reason": "job_id has invalid format",
            "job_id": job_id,
            "reservation_key": reservation_key,
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if reservation_key is not None and not reservations.is_reservation_key(reservation_key):
        return _json_tool_response({
            "status": "refused",
            "action": normalized_action,
            "reason": "reservation_key must match <canonical-host>.gpu<gpu-index>",
            "job_id": job_id,
            "reservation_key": reservation_key,
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if normalized_action in {"stop", "retry", "finish", "update_cadence"} and not HEARTBEAT_MANAGER.is_healthy():
        return _json_tool_response({
            "status": "refused",
            "action": normalized_action,
            "reason": "heartbeat manager is unhealthy; owner-side lifecycle actions are unsafe",
            "job_id": job_id,
            "reservation_key": reservation_key,
            "heartbeat_manager_healthy": False,
            "heartbeat_manager_reason": HEARTBEAT_MANAGER.health_reason(),
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    job_record, resolved_by, candidates = _resolve_job_target(job_id, reservation_key)
    if candidates:
        return _json_tool_response({
            "status": "ambiguous_target",
            "action": normalized_action,
            "reason": "multiple current-repo reservations match; choose a job_id or reservation_key",
            "resolved_by": resolved_by,
            "candidates": candidates,
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if job_record is not None:
        if normalized_action == "stop":
            return _stop_owned_job(normalized_action, job_record)
        if normalized_action == "retry":
            return _retry_owned_job(job_record)
        if normalized_action == "finish":
            return _finish_owned_job(job_record)
        if normalized_action == "update_cadence":
            return _update_owned_job_cadence(
                job_record,
                expected_duration_sec=expected_duration_sec,
                cadence_hint_sec=cadence_hint_sec,
                reason=reason,
            )
        return _job_status_response(
            action=normalized_action,
            job_record=job_record,
            reason="",
            early_poll_reason=early_poll_reason,
            allow_compact_early=True,
        )
    return _empty_status_response(
        action=normalized_action,
        reason="no managed GPU job matched the requested target",
    )


@mcp.tool()
def manage_gpu_job(
    action: str = "status",
    job_id: Optional[str] = None,
    reservation_key: Optional[str] = None,
    expected_duration_sec: Optional[int] = None,
    cadence_hint_sec: Optional[int] = None,
    reason: Optional[str] = None,
    early_poll_reason: Optional[str] = None,
):
    """Inspect or change a managed GPU job."""
    kwargs = {
        "action": action,
        "job_id": job_id,
        "reservation_key": reservation_key,
        "expected_duration_sec": expected_duration_sec,
        "cadence_hint_sec": cadence_hint_sec,
        "reason": reason,
        "early_poll_reason": early_poll_reason,
    }
    _phase7_trace_tool_call("manage_gpu_job", kwargs)
    response = _manage_gpu_job_impl(**kwargs)
    _phase7_trace_tool_result("manage_gpu_job", response)
    return response


@mcp.tool()
def list_gpu_reservations(scope: str = "mine", fresh: bool = False):
    """List active GPU reservations for recovery or diagnostics.

    `fresh` is a bounded refresh request, not a filter. Phase 0 only exposes the
    stable read/report shape; stale inspection and cleanup are added later.
    """
    _apply_test_controls_from_file()
    if stale := _stale_policy_refusal():
        return stale
    if scope not in {"mine", "all"}:
        return _json_tool_response({
            "status": "refused",
            "reason": "scope must be 'mine' or 'all'",
            "scope": scope,
            "fresh": fresh,
            "server_instance_id": SERVER_INSTANCE_ID,
        })
    if not isinstance(fresh, bool):
        return _json_tool_response({
            "status": "refused",
            "reason": "fresh must be a boolean",
            "scope": scope,
            "fresh": fresh,
            "server_instance_id": SERVER_INSTANCE_ID,
        })

    registry_root = reservations.reservation_registry_root()
    registry_status, registry_error, rows_by_key = _load_reservation_rows(scope=scope)
    if fresh and registry_status == "ok":
        rows_by_key = _refresh_stale_reservations(rows_by_key)

    return _json_tool_response({
        "status": "ok" if registry_status == "ok" else "error",
        "scope": scope,
        "fresh": fresh,
        "fresh_semantics": "bounded_refresh_not_filter",
        "registry_root": str(registry_root),
        "registry_status": registry_status,
        "registry_error": registry_error,
        "reservations": [rows_by_key[key] for key in sorted(rows_by_key)],
        "server_instance_id": SERVER_INSTANCE_ID,
    })


def _run_python_on_gpu_impl(
    host: str,
    gpu_index: int,
    script_path: str,
    args: Optional[list[str]] = None,
    async_mode: Optional[bool] = None,
    output_file: Optional[str] = None,
    job_role: Optional[str] = None,
    expected_duration_sec: Optional[int] = None,
    cadence_hint_sec: Optional[int] = None,
    smoke_job_id: Optional[str] = None,
    smoke_cadence_representative: bool = False,
    smoke_skip_reason: Optional[str] = None,
):
    """Launch an approved Python file as a managed GPU job.

    Args:
        host: Configured hostname.
        gpu_index: GPU device index to use (sets CUDA_VISIBLE_DEVICES).
        script_path: Existing .py file under an approved script root.
        args: Positional CLI args passed to the Python script.
        async_mode: Compatibility input. Both true and false return a managed handle.
        output_file: Approved path for stdout/stderr capture.
        job_role: Optional Phase 7 role: smoke, main, or one_off.
        expected_duration_sec: Optional runtime estimate for initial cadence.
        cadence_hint_sec: Optional direct cadence hint, clamped to 60..3600.
        smoke_job_id: Optional same-repo smoke job evidence for a main launch.
        smoke_cadence_representative: Whether smoke runtime should drive cadence.
        smoke_skip_reason: Optional reason to launch a main job without smoke evidence.

    Returns:
        JSON managed job handle.
    """
    _apply_test_controls_from_file()
    if stale := _stale_policy_refusal():
        return stale
    if not HEARTBEAT_MANAGER.is_healthy():
        return _launch_refusal(
            "heartbeat manager is unhealthy; new launches are unsafe",
            heartbeat_manager_healthy=False,
            heartbeat_manager_reason=HEARTBEAT_MANAGER.health_reason(),
        )
    if not isinstance(gpu_index, int) or isinstance(gpu_index, bool) or gpu_index < 0:
        return _launch_refusal("gpu_index must be a non-negative integer", gpu_index=gpu_index)
    if args is not None and (
        not isinstance(args, list)
        or any(not isinstance(arg, (str, int, float, bool)) or arg is None for arg in args)
    ):
        return _launch_refusal("args must be a list of string/number/boolean values", gpu_index=gpu_index)
    if async_mode is not None and not isinstance(async_mode, bool):
        return _launch_refusal("async_mode must be a boolean", gpu_index=gpu_index)
    if output_file is not None and not isinstance(output_file, str):
        return _launch_refusal("output_file must be a string path", gpu_index=gpu_index)
    resolved_job_role, job_role_defaulted, role_error = _resolve_job_role(job_role, async_mode)
    if role_error:
        return _launch_refusal(role_error, host=host, gpu_index=gpu_index)
    expected_duration, duration_error = _validate_positive_int(
        expected_duration_sec,
        name="expected_duration_sec",
    )
    if duration_error:
        return _launch_refusal(duration_error, host=host, gpu_index=gpu_index)
    cadence_hint, cadence_error = _validate_positive_int(cadence_hint_sec, name="cadence_hint_sec")
    if cadence_error:
        return _launch_refusal(cadence_error, host=host, gpu_index=gpu_index)
    if not isinstance(smoke_cadence_representative, bool):
        return _launch_refusal(
            "smoke_cadence_representative must be a boolean",
            host=host,
            gpu_index=gpu_index,
        )
    smoke_skip_text = _nonempty_text(smoke_skip_reason)
    phase7_inputs_present = any(
        value is not None
        for value in [
            async_mode,
            job_role,
            expected_duration_sec,
            cadence_hint_sec,
            smoke_job_id,
            smoke_skip_reason,
        ]
    ) or smoke_cadence_representative
    phase7_workflow_guard = any(
        value is not None
        for value in [
            job_role,
            expected_duration_sec,
            cadence_hint_sec,
            smoke_job_id,
            smoke_skip_reason,
        ]
    ) or smoke_cadence_representative
    heartbeat_interval_sec, cadence_basis, positive_smoke = _phase7_cadence_basis(
        job_role=str(resolved_job_role),
        expected_duration_sec=expected_duration,
        cadence_hint_sec=cadence_hint,
        smoke_job_id=smoke_job_id,
        smoke_cadence_representative=smoke_cadence_representative,
        smoke_skip_reason=smoke_skip_text,
    )
    if not phase7_inputs_present:
        heartbeat_interval_sec = reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC
        cadence_basis = {
            "source": "legacy_fixed_cadence",
            "selected_interval_sec": reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC,
        }
    phase7_response_fields = {
        "heartbeat_interval_sec": heartbeat_interval_sec,
        "job_role": resolved_job_role,
        "job_role_defaulted": job_role_defaulted,
        "cadence_basis": cadence_basis,
    }
    async_mode_requested = bool(async_mode)
    if phase7_workflow_guard and resolved_job_role == "main" and not positive_smoke and smoke_skip_text is None:
        return _launch_refusal(
            (
                "main job launched without smoke evidence; run a job_role=\"smoke\" "
                "check first or provide smoke_skip_reason"
            ),
            host=host,
            gpu_index=gpu_index,
            job_role=resolved_job_role,
            job_role_defaulted=job_role_defaulted,
            cadence_basis=cadence_basis,
        )
    if not _is_allowed_host(host):
        return _launch_refusal(
            "host must be one of the configured GPU MCP NODES",
            host=host,
            gpu_index=gpu_index,
        )

    try:
        canonical_host = _canonical_policy_host(host)
        reservation_key_value = reservations.reservation_key(canonical_host, gpu_index)
        argv = _build_python_gpu_argv(script_path, args=args, execution_host=host)
        out_path = _validate_output_path(output_file)
    except ValueError as e:
        message = str(e)
        return _launch_refusal(message, host=host, gpu_index=gpu_index)

    try:
        _prepare_output_parent(out_path)
    except ValueError as e:
        return _launch_refusal(str(e), host=canonical_host, gpu_index=gpu_index, reservation_key=reservation_key_value)

    job_id = reservations.generate_job_id()
    attempt_id = reservations.generate_attempt_id()
    next_poll_after = _next_poll_after(heartbeat_interval_sec)
    job_env = _gpu_job_env(gpu_index)
    process_nonce = secrets.token_urlsafe(18)
    process_fingerprint = _managed_process_fingerprint(job_id, attempt_id, canonical_host, process_nonce)
    job_env["GPU_MCP_PROCESS_FINGERPRINT"] = process_fingerprint
    script = _validate_python_script_path(script_path)
    registry_root = reservations.reservation_registry_root()
    outcome_path = reservations.outcome_record_path(REPO_ROOT, job_id, attempt_id)
    outcome_base = {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": canonical_host,
        "gpu_index": gpu_index,
        "remote_pid": None,
        "terminal_status": "launcher_error",
        "exit_code": None,
        "signal": None,
        "started_at": None,
        "ended_at": None,
        "error_summary": None,
    }
    initial_metadata = reservations.build_shared_metadata(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key_value,
        host=canonical_host,
        gpu_index=gpu_index,
        repo=REPO_ROOT,
        script_path=script,
        owner_user=GPU_MCP_USER,
        server_instance_id=SERVER_INSTANCE_ID,
        remote_pid=None,
        process_fingerprint=None,
        heartbeat_interval_sec=heartbeat_interval_sec,
    )
    try:
        acquired, acquire_reason = reservations.acquire_reservation(
            registry_root=registry_root,
            reservation_key_value=reservation_key_value,
            metadata=initial_metadata,
        )
    except Exception as e:
        return _launch_refusal(
            f"failed to acquire reservation: {e}",
            host=canonical_host,
            gpu_index=gpu_index,
            reservation_key=reservation_key_value,
        )
    if not acquired:
        existing = _read_reservation_metadata(reservation_key_value)
        if existing is not None and _metadata_stale(existing):
            cleaned, _inspection = _cleanup_stale_gone_reservation(reservation_key_value, existing)
            if cleaned:
                try:
                    acquired, acquire_reason = reservations.acquire_reservation(
                        registry_root=registry_root,
                        reservation_key_value=reservation_key_value,
                        metadata=initial_metadata,
                    )
                except Exception as e:
                    return _launch_refusal(
                        f"failed to acquire reservation after stale cleanup: {e}",
                        host=canonical_host,
                        gpu_index=gpu_index,
                        reservation_key=reservation_key_value,
                    )
        if acquired:
            pass
        else:
            return _launch_refusal(
                acquire_reason or "GPU is already reserved",
                host=canonical_host,
                gpu_index=gpu_index,
                reservation_key=reservation_key_value,
            )

    job_record = reservations.build_job_record(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key_value,
        host=canonical_host,
        gpu_index=gpu_index,
        script_path=script,
        args=args,
        output_file=out_path,
        server_instance_id=SERVER_INSTANCE_ID,
        next_poll_after=next_poll_after,
    )
    job_record.update({
        "job_role": resolved_job_role,
        "job_role_defaulted": job_role_defaulted,
        "heartbeat_interval_sec": heartbeat_interval_sec,
        "cadence_basis": cadence_basis,
        "phase7_poll_discipline": phase7_inputs_present,
    })
    if smoke_job_id is not None:
        job_record["smoke_job_id"] = smoke_job_id
    if smoke_cadence_representative:
        job_record["smoke_cadence_representative"] = True
    if smoke_skip_text is not None:
        job_record["smoke_skip_reason"] = smoke_skip_text
    if expected_duration is not None:
        job_record["expected_duration_sec"] = expected_duration
    if cadence_hint is not None:
        job_record["cadence_hint_sec"] = cadence_hint
    try:
        reservations.atomic_write_json(reservations.job_record_path(REPO_ROOT, job_id), job_record)
    except Exception as e:
        reservations.remove_reservation_if_launch_failed(
            registry_root=registry_root,
            reservation_key_value=reservation_key_value,
        )
        return _launch_refusal(
            f"failed to record managed job state before launch: {e}",
            host=canonical_host,
            gpu_index=gpu_index,
            reservation_key=reservation_key_value,
        )
    HEARTBEAT_MANAGER.register(
        reservation_key_value,
        interval_sec=heartbeat_interval_sec,
    )

    if _is_local_host(host):
        try:
            env = os.environ.copy()
            env.update(job_env)
            supervisor_argv = _managed_supervisor_argv(
                argv,
                out_path,
                job_env,
                outcome_path,
                outcome_base,
            )
            proc = subprocess.Popen(
                supervisor_argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=str(REPO_ROOT),
                start_new_session=True,
            )
        except Exception as e:
            reservations.remove_reservation_if_launch_failed(
                registry_root=registry_root,
                reservation_key_value=reservation_key_value,
            )
            HEARTBEAT_MANAGER.unregister(reservation_key_value)
            shutil.rmtree(reservations.job_record_path(REPO_ROOT, job_id).parent, ignore_errors=True)
            return _launch_refusal(
                f"Failed to launch local GPU job on {host}: {e}",
                host=canonical_host,
                gpu_index=gpu_index,
                reservation_key=reservation_key_value,
            )
        pid_value = str(proc.pid)
        launch_mode = "local"
        remote_start_time = _process_start_time(host, int(pid_value))
    else:
        launch_cmd = _remote_async_launch_command(argv, out_path, job_env, outcome_path, outcome_base)
        bg_cmd = _remote_repo_command(launch_cmd)
        pid_str = _ssh_run(host, bg_cmd)
        if pid_str is None:
            return _launch_handle_response(
                status="launch_outcome_unknown",
                reason=f"SSH launch did not return a PID from {host}; reservation kept fail-closed",
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key_value,
                host=canonical_host,
                gpu_index=gpu_index,
                output_path=out_path,
                next_poll_after=next_poll_after,
                job_lifecycle="launch_outcome_unknown",
                launch="ssh",
                async_mode_requested=async_mode_requested,
                **phase7_response_fields,
            )
        pid_value = pid_str.strip()
        if not pid_value.isdigit() or int(pid_value) <= 0:
            return _launch_handle_response(
                status="launch_outcome_unknown",
                reason=(
                    f"invalid async pid returned from {host}: {pid_value!r}; "
                    "reservation kept fail-closed"
                ),
                job_id=job_id,
                attempt_id=attempt_id,
                reservation_key_value=reservation_key_value,
                host=canonical_host,
                gpu_index=gpu_index,
                output_path=out_path,
                next_poll_after=next_poll_after,
                job_lifecycle="launch_outcome_unknown",
                launch="ssh",
                async_mode_requested=async_mode_requested,
                **phase7_response_fields,
            )
        launch_mode = "ssh"
        remote_start_time = None

    job_record.update({
        "remote_pid": int(pid_value),
        "remote_start_time": remote_start_time,
        "process_fingerprint": process_fingerprint,
        "launch_mode": launch_mode,
        "async_mode_requested": async_mode_requested,
        "outcome_file": str(outcome_path),
    })
    launched_metadata = dict(initial_metadata)
    launched_metadata.update({
        "remote_pid": int(pid_value),
        "remote_start_time": remote_start_time,
        "process_fingerprint": process_fingerprint,
        "last_heartbeat_at": reservations.iso_timestamp(),
    })
    try:
        reservations.atomic_write_json(reservations.job_record_path(REPO_ROOT, job_id), job_record)
        HEARTBEAT_MANAGER.update_owned_metadata(reservation_key_value, {
            "remote_pid": int(pid_value),
            "remote_start_time": remote_start_time,
            "process_fingerprint": process_fingerprint,
            "last_heartbeat_at": launched_metadata["last_heartbeat_at"],
        })
    except Exception as e:
        return _launch_handle_response(
            status="launched_with_warning",
            reason=f"failed to record managed job state after launch: {e}",
            job_id=job_id,
            attempt_id=attempt_id,
            reservation_key_value=reservation_key_value,
            host=canonical_host,
            gpu_index=gpu_index,
            output_path=out_path,
            next_poll_after=next_poll_after,
            job_lifecycle="running",
            launch=launch_mode,
            async_mode_requested=async_mode_requested,
            **phase7_response_fields,
            process={
                "remote_pid": int(pid_value),
                "remote_start_time": remote_start_time,
                "remote_boot_id": None,
                "process_fingerprint": process_fingerprint,
            },
        )

    return _launch_handle_response(
        status="launched",
        reason="",
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key_value,
        host=canonical_host,
        gpu_index=gpu_index,
        output_path=out_path,
        next_poll_after=next_poll_after,
        job_lifecycle="running",
        launch=launch_mode,
        async_mode_requested=async_mode_requested,
        **phase7_response_fields,
        process={
            "remote_pid": int(pid_value),
            "remote_start_time": remote_start_time,
            "remote_boot_id": None,
            "process_fingerprint": process_fingerprint,
        },
    )


@mcp.tool()
def run_python_on_gpu(
    host: str,
    gpu_index: int,
    script_path: str,
    args: Optional[list[str]] = None,
    async_mode: Optional[bool] = None,
    output_file: Optional[str] = None,
    job_role: Optional[str] = None,
    expected_duration_sec: Optional[int] = None,
    cadence_hint_sec: Optional[int] = None,
    smoke_job_id: Optional[str] = None,
    smoke_cadence_representative: bool = False,
    smoke_skip_reason: Optional[str] = None,
):
    """Launch an approved Python file as a managed GPU job."""
    kwargs = {
        "host": host,
        "gpu_index": gpu_index,
        "script_path": script_path,
        "args": args,
        "async_mode": async_mode,
        "output_file": output_file,
        "job_role": job_role,
        "expected_duration_sec": expected_duration_sec,
        "cadence_hint_sec": cadence_hint_sec,
        "smoke_job_id": smoke_job_id,
        "smoke_cadence_representative": smoke_cadence_representative,
        "smoke_skip_reason": smoke_skip_reason,
    }
    _phase7_trace_tool_call("run_python_on_gpu", kwargs)
    response = _run_python_on_gpu_impl(**kwargs)
    _phase7_trace_tool_result("run_python_on_gpu", response)
    return response


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
    def _stop_heartbeat_and_exit(signum, frame):
        HEARTBEAT_MANAGER.stop()
        raise SystemExit(128 + int(signum))

    signal_lib.signal(signal_lib.SIGTERM, _stop_heartbeat_and_exit)
    signal_lib.signal(signal_lib.SIGINT, _stop_heartbeat_and_exit)
    try:
        mcp.run(transport="stdio")
    finally:
        HEARTBEAT_MANAGER.stop()
