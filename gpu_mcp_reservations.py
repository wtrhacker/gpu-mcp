from __future__ import annotations

import json
import os
import re
import secrets
import socket
import contextlib
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE

MIN_HEARTBEAT_INTERVAL_SEC = SECONDS_PER_MINUTE
DEFAULT_HEARTBEAT_INTERVAL_SEC = 10 * SECONDS_PER_MINUTE
MAX_HEARTBEAT_INTERVAL_SEC = SECONDS_PER_HOUR
HEARTBEAT_MANAGER_TICK_SEC = 1
HEARTBEAT_WRITE_INTERVAL_CAP_SEC = SECONDS_PER_MINUTE
STALE_MULTIPLIER = 3

# Polling is agent guidance, not lease safety. Direct hints may select any
# positive integer number of seconds; these values are no-hint fallbacks only.
MIN_POLL_INTERVAL_SEC = 1
DEFAULT_SMOKE_POLL_INTERVAL_SEC = 5 * SECONDS_PER_MINUTE
DEFAULT_POLL_INTERVAL_SEC = SECONDS_PER_HOUR

DEFAULT_RESERVATION_REGISTRY_ROOT = Path.home() / "gpu-mcp" / "state" / "reservations"
TEST_RESERVATION_ROOT_ENV = "GPU_MCP_TEST_RESERVATION_ROOT"
REPO_JOB_STATE_RELATIVE = Path(".gpu_mcp_state") / "jobs"

JOB_ID_PREFIX = "job"
ATTEMPT_ID_PREFIX = "attempt"
SERVER_INSTANCE_ID_PREFIX = "server"

_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ID_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_JOB_ID_RE = re.compile(r"^job-\d{8}T\d{6}Z-[A-Za-z0-9_-]+$")
_ATTEMPT_ID_RE = re.compile(r"^attempt-\d{8}T\d{6}Z-[A-Za-z0-9_-]+$")
_SERVER_INSTANCE_ID_RE = re.compile(r"^server-[A-Za-z0-9._-]+-\d+-[A-Za-z0-9_-]+$")
_RESERVATION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.gpu\d+$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def iso_timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _random_suffix(bytes_count: int = 6) -> str:
    return secrets.token_urlsafe(bytes_count).rstrip("=")


def _validate_suffix(suffix: str) -> str:
    if not isinstance(suffix, str) or not suffix or not _ID_SUFFIX_RE.match(suffix):
        raise ValueError("id suffix must be a non-empty URL-safe token")
    return suffix


def generate_job_id(*, now: datetime | None = None, suffix: str | None = None) -> str:
    token = _validate_suffix(suffix) if suffix is not None else _random_suffix()
    return f"{JOB_ID_PREFIX}-{utc_timestamp(now)}-{token}"


def generate_attempt_id(*, now: datetime | None = None, suffix: str | None = None) -> str:
    token = _validate_suffix(suffix) if suffix is not None else _random_suffix()
    return f"{ATTEMPT_ID_PREFIX}-{utc_timestamp(now)}-{token}"


def generate_server_instance_id(
    *,
    hostname: str | None = None,
    pid: int | None = None,
    suffix: str | None = None,
) -> str:
    host = canonical_host_component(hostname or socket.gethostname())
    process_id = os.getpid() if pid is None else pid
    if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
        raise ValueError("pid must be a positive integer")
    token = _validate_suffix(suffix) if suffix is not None else _random_suffix(9)
    return f"{SERVER_INSTANCE_ID_PREFIX}-{host}-{process_id}-{token}"


def is_job_id(value: object) -> bool:
    return isinstance(value, str) and bool(_JOB_ID_RE.match(value))


def is_attempt_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ATTEMPT_ID_RE.match(value))


def is_server_instance_id(value: object) -> bool:
    return isinstance(value, str) and bool(_SERVER_INSTANCE_ID_RE.match(value))


def canonical_host_component(host: str) -> str:
    if not isinstance(host, str):
        raise ValueError("host must be a string")
    value = host.split("@")[-1].strip().lower()
    if not value:
        raise ValueError("host must not be empty")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError("host must be a safe single path component")
    if not _SAFE_HOST_RE.match(value):
        raise ValueError("host must contain only letters, numbers, dot, underscore, or dash")
    return value


def reservation_key(canonical_host: str, gpu_index: int) -> str:
    host = canonical_host_component(canonical_host)
    if isinstance(gpu_index, bool) or not isinstance(gpu_index, int) or gpu_index < 0:
        raise ValueError("gpu_index must be a non-negative integer")
    return f"{host}.gpu{gpu_index}"


def is_reservation_key(value: object) -> bool:
    if not isinstance(value, str) or not _RESERVATION_KEY_RE.match(value):
        return False
    host, gpu_text = value.rsplit(".gpu", 1)
    try:
        return reservation_key(host, int(gpu_text)) == value
    except ValueError:
        return False


def reservation_registry_root(*, env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    injected = source.get(TEST_RESERVATION_ROOT_ENV, "").strip()
    if injected and source.get("PYTEST_CURRENT_TEST"):
        return Path(injected).expanduser().absolute()
    return DEFAULT_RESERVATION_REGISTRY_ROOT


def repo_job_state_root(repo_root: str | Path) -> Path:
    return Path(repo_root).expanduser().resolve() / REPO_JOB_STATE_RELATIVE


def job_record_path(repo_root: str | Path, job_id: str) -> Path:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    return repo_job_state_root(repo_root) / job_id / "job.json"


def outcome_record_path(repo_root: str | Path, job_id: str, attempt_id: str) -> Path:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    if not is_attempt_id(attempt_id):
        raise ValueError("attempt_id has invalid format")
    return repo_job_state_root(repo_root) / job_id / "attempts" / attempt_id / "outcome.json"


def hook_reminder_path(repo_root: str | Path, job_id: str) -> Path:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    return Path(repo_root).expanduser().resolve() / ".gpu_mcp_state" / "hook_reminders" / f"{job_id}.json"


def _assert_no_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path(".")
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise OSError(f"refusing path with symlink component: {current}")


def reservation_dir(registry_root: str | Path, reservation_key_value: str) -> Path:
    if not is_reservation_key(reservation_key_value):
        raise ValueError("reservation_key has invalid format")
    return Path(registry_root).expanduser().absolute() / reservation_key_value


def acquire_reservation(
    *,
    registry_root: str | Path,
    reservation_key_value: str,
    metadata: dict[str, Any],
) -> tuple[bool, str]:
    root = Path(registry_root).expanduser().absolute()
    if root.exists() and root.is_symlink():
        raise OSError(f"reservation registry root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = reservation_dir(root, reservation_key_value)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return False, "reservation already exists"
    try:
        atomic_write_json(path / "metadata.json", metadata)
    except Exception:
        try:
            path.rmdir()
        except OSError:
            pass
        raise
    return True, ""


def remove_reservation_if_launch_failed(
    *,
    registry_root: str | Path,
    reservation_key_value: str,
) -> None:
    path = reservation_dir(registry_root, reservation_key_value)
    metadata = path / "metadata.json"
    try:
        if not metadata.is_symlink() and metadata.exists():
            metadata.unlink()
        path.rmdir()
    except FileNotFoundError:
        return


def quarantine_reservation(
    *,
    registry_root: str | Path,
    reservation_key_value: str,
    suffix: str,
) -> Path:
    source = reservation_dir(registry_root, reservation_key_value)
    quarantine_root = Path(registry_root).expanduser().absolute() / ".quarantine"
    _assert_no_symlink_ancestors(quarantine_root)
    quarantine_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_ancestors(quarantine_root)
    target = quarantine_root / f"{reservation_key_value}.{suffix}"
    os.replace(source, target)
    return target


@contextlib.contextmanager
def cleanup_finalization_guard(registry_root: str | Path, reservation_key_value: str):
    if not is_reservation_key(reservation_key_value):
        raise ValueError("reservation_key has invalid format")
    root = Path(registry_root).expanduser().absolute()
    locks_dir = root / ".locks"
    _assert_no_symlink_ancestors(locks_dir)
    locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_ancestors(locks_dir)
    lock_path = locks_dir / f"{reservation_key_value}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def atomic_write_json(path: str | Path, data: dict[str, Any]) -> None:
    target = Path(path)
    _assert_no_symlink_ancestors(target.parent)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_ancestors(target.parent)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def read_json_file_no_follow(path: str | Path) -> Any:
    target = Path(path)
    if target.is_symlink():
        raise OSError(f"refusing to read symlink: {target}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags)
    with os.fdopen(fd, "r") as handle:
        return json.load(handle)


def _script_name(script_path: str | Path) -> str:
    return Path(str(script_path)).name


def build_shared_metadata(
    *,
    job_id: str,
    reservation_key_value: str,
    host: str,
    gpu_index: int,
    repo: str | Path,
    script_path: str | Path,
    owner_user: str,
    server_instance_id: str,
    attempt_id: str | None = None,
    remote_pid: int | None = None,
    remote_start_time: str | None = None,
    remote_boot_id: str | None = None,
    process_fingerprint: str | None = None,
    reserved_at: str | None = None,
    last_heartbeat_at: str | None = None,
    heartbeat_interval_sec: int = DEFAULT_HEARTBEAT_INTERVAL_SEC,
) -> dict[str, Any]:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    if attempt_id is not None and not is_attempt_id(attempt_id):
        raise ValueError("attempt_id has invalid format")
    if not is_server_instance_id(server_instance_id):
        raise ValueError("server_instance_id has invalid format")
    if reservation_key_value != reservation_key(host, gpu_index):
        raise ValueError("reservation_key must match host and gpu_index")
    if not (MIN_HEARTBEAT_INTERVAL_SEC <= heartbeat_interval_sec <= MAX_HEARTBEAT_INTERVAL_SEC):
        raise ValueError("heartbeat_interval_sec is outside the allowed range")
    if remote_pid is not None and (isinstance(remote_pid, bool) or remote_pid <= 0):
        raise ValueError("remote_pid must be positive or null")

    timestamp = iso_timestamp()
    return {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": canonical_host_component(host),
        "gpu_index": gpu_index,
        "repo": str(Path(repo).expanduser().resolve()),
        "script_name": _script_name(script_path),
        "owner_user": owner_user,
        "server_instance_id": server_instance_id,
        "remote_pid": remote_pid,
        "remote_start_time": remote_start_time,
        "remote_boot_id": remote_boot_id,
        "process_fingerprint": process_fingerprint,
        "reserved_at": reserved_at or timestamp,
        "last_heartbeat_at": last_heartbeat_at or timestamp,
        "heartbeat_interval_sec": heartbeat_interval_sec,
    }


def build_job_record(
    *,
    job_id: str,
    attempt_id: str,
    reservation_key_value: str,
    host: str,
    gpu_index: int,
    script_path: str | Path,
    args: list[Any] | None,
    output_file: str | Path | None,
    server_instance_id: str,
    next_poll_after: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    if not is_attempt_id(attempt_id):
        raise ValueError("attempt_id has invalid format")
    if not is_server_instance_id(server_instance_id):
        raise ValueError("server_instance_id has invalid format")
    return {
        "schema_version": 1,
        "job_id": job_id,
        "active_attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": canonical_host_component(host),
        "gpu_index": gpu_index,
        "script_path": str(Path(script_path).expanduser().resolve()),
        "script_name": _script_name(script_path),
        "args": [str(arg) for arg in (args or [])],
        "output_file": None if output_file is None else str(Path(output_file).expanduser().resolve()),
        "server_instance_id": server_instance_id,
        "created_at": created_at or iso_timestamp(),
        "next_poll_after": next_poll_after,
        "last_status_checked_at": None,
    }


def build_outcome_record(
    *,
    job_id: str,
    attempt_id: str,
    reservation_key_value: str,
    host: str,
    gpu_index: int,
    terminal_status: str,
    ended_at: str,
    remote_pid: int | None = None,
    exit_code: int | None = None,
    signal: str | None = None,
    started_at: str | None = None,
    error_summary: str | None = None,
) -> dict[str, Any]:
    if not is_job_id(job_id):
        raise ValueError("job_id has invalid format")
    if not is_attempt_id(attempt_id):
        raise ValueError("attempt_id has invalid format")
    if reservation_key_value != reservation_key(host, gpu_index):
        raise ValueError("reservation_key must match host and gpu_index")
    if remote_pid is not None and (isinstance(remote_pid, bool) or remote_pid <= 0):
        raise ValueError("remote_pid must be positive or null")
    if terminal_status not in {"success", "failure", "signaled", "launcher_error"}:
        raise ValueError("terminal_status has invalid value")
    return {
        "schema_version": 1,
        "job_id": job_id,
        "attempt_id": attempt_id,
        "reservation_key": reservation_key_value,
        "host": canonical_host_component(host),
        "gpu_index": gpu_index,
        "remote_pid": remote_pid,
        "terminal_status": terminal_status,
        "exit_code": exit_code,
        "signal": signal,
        "started_at": started_at,
        "ended_at": ended_at,
        "error_summary": error_summary,
    }
