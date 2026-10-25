#!/usr/bin/env python3
"""Codex's stdio connection to one automatically started backend per repo."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from gpu_mcp_codex_state import private_directory, runtime_root
import gpu_mcp_reservations as reservations

PROTOCOL_VERSION = 1


def code_hash() -> str:
    root = Path(__file__).resolve().parent
    names = ("gpu_mcp_backend.py", "gpu_mcp_server.py", "gpu_mcp_builds.py", "gpu_mcp_config.py", "gpu_mcp_guard.py",
             "gpu_mcp_reservations.py", "gpu_mcp_codex_state.py", "gpu_mcp_codex_runtime.py",
             "gpu_mcp_codex_daemon.py", "gpu_mcp_codex_host.py", "gpu_mcp_build_worker.py",
             "gpu_mcp_policy_approval.py", "gpu_mcp_safe_runner.py", "gpu_mcp_codex_launcher.py")
    names += ("gpu_mcp_codex_versions.py",)
    names += ("gpu_mcp_mps.py", "gpu_mcp_mps_worker.py", "gpu_mcp_mps_inspection.py")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def process_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        return None if fields[0] in {"Z", "X"} else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def runtime_signature():
    """Reject silent reuse with a different interpreter, identity or daemon."""
    settings = {"runtime_root": str(Path(__file__).resolve().parent),
                "server_python": str(Path(sys.executable).resolve()),
                "job_python": os.environ.get("GPU_MCP_PYTHON", "").strip() or sys.executable,
                "job_user": os.environ.get("GPU_MCP_USER", "").strip() or
                            os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name,
                "ssh_key": os.environ.get("GPU_MCP_SSH_KEY", ""),
                "ssh_fallback": os.environ.get("GPU_MCP_ALLOW_SSH_FALLBACK", ""),
                "codex_home": str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve())}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        for key in ("GPU_MCP_TEST_RESERVATION_ROOT", "GPU_MCP_TEST_BUILD_CONTROL_ROOT", "GPU_MCP_TEST_BUILD_WORKER_ROOT"):
            settings[key] = os.environ.get(key)
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def request(info, path, *, payload=None, human=False, timeout=120):
    token = info["human_token" if human else "agent_token"]
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(info["url"] + path, data=data, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        return json.loads(body) if body.strip() else None


def read_info(root: Path):
    try:
        info = reservations.read_json_file_no_follow(root / "backend.json")
    except FileNotFoundError:
        return None
    if (not isinstance(info, dict) or type(info.get("pid")) is not int or info["pid"] <= 0
            or any(not isinstance(info.get(key), str) or not info[key]
                   for key in ("hostname", "process_start", "server_instance_id", "config_path"))):
        raise ValueError(f"Invalid shared GPU MCP backend identity: {root / 'backend.json'}")
    return info


def alive(info):
    if (not isinstance(info, dict) or info.get("hostname") != socket.gethostname()
            or type(info.get("pid")) is not int or info["pid"] <= 0
            or not info.get("process_start")):
        return False
    observed_start = process_start(info["pid"])
    if observed_start is not None:
        return observed_start == info["process_start"]
    # An agent's PID namespace may hide a backend on the same control host.
    # Its authenticated endpoint can still establish the recorded identity.
    if not all(info.get(key) for key in ("url", "agent_token", "server_instance_id")):
        return False
    try:
        health = request(info, "/health", timeout=2)
    except (urllib.error.URLError, OSError) as exc:
        cause = getattr(exc, "reason", exc)
        if isinstance(cause, ConnectionRefusedError):
            return False
        raise RuntimeError("Cannot verify the existing GPU MCP backend; no replacement was started.") from exc
    if health.get("server_instance_id") != info["server_instance_id"]:
        raise RuntimeError("shared backend identity changed")
    return True


def stop_if_idle(info, *, timeout=15):
    """Ask the owning backend to stop atomically, or leave busy work running.

    Only the backend can decide whether a request or job is still active.
    A refusal is normal during an update; it does not make the connection bad.
    """
    result = request(info, "/human/stop-if-idle", payload={}, human=True, timeout=3)
    if result.get("status") == "refused":
        return False
    if result.get("status") != "stopping":
        raise RuntimeError("The backend did not confirm an idle shutdown.")
    deadline = time.monotonic() + timeout
    while alive(info) and time.monotonic() < deadline:
        time.sleep(0.05)
    if alive(info):
        raise TimeoutError("The idle backend is still shutting down; reconnect shortly. No process was killed.")
    return True


def backend_status(config, *, base=None):
    """Describe installed and loaded code without starting or stopping anything."""
    config = Path(config).expanduser().resolve()
    installed = code_hash()
    info = read_info(runtime_root(config, base=base))
    result = {"config_path": str(config), "installed_runtime": str(Path(__file__).resolve().parent),
              "installed_code_hash": installed, "status": "not_running"}
    if alive(info):
        health = request(info, "/health", timeout=3)
        if health.get("server_instance_id") != info["server_instance_id"]:
            raise RuntimeError("shared backend identity changed")
        result.update(status="running", pid=info["pid"], server_instance_id=info["server_instance_id"],
                      backend_code_hash=info.get("code_hash"),
                      update_pending=info.get("code_hash") != installed,
                      protocol_matches=info.get("protocol_version") == PROTOCOL_VERSION,
                      runtime_settings_match=info.get("runtime_signature") == runtime_signature(),
                      policy_state=health.get("policy_state"))
    return result


def ensure_backend(config: str | Path, *, base: str | Path | None = None, timeout=20):
    config = Path(config).expanduser()
    if not config.is_absolute() or config.is_symlink():
        raise ValueError("--config requires an absolute non-symlink policy path")
    config = config.resolve()
    root = runtime_root(config, base=base)
    private_directory(root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(root / "startup.lock", flags, 0o600), "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        info = read_info(root)
        if alive(info):
            if info.get("config_path") != str(config):
                raise RuntimeError("This repository already has a backend for a different policy path.")
            if info.get("protocol_version") != PROTOCOL_VERSION:
                raise RuntimeError("The shared backend uses an incompatible connection protocol. Finish its jobs before upgrading it.")
            if info.get("runtime_signature") != runtime_signature():
                raise RuntimeError("The shared backend has different runtime settings. Drain its jobs and stop the idle backend before changing interpreters or connection settings.")
            health = request(info, "/health", timeout=3)
            if health.get("server_instance_id") != info.get("server_instance_id"):
                raise RuntimeError("shared backend identity changed")
            if info.get("code_hash") == code_hash():
                return info
            # A source edit is not a wire-protocol change. Keep the actual
            # backend's tools and job ownership available until it can refresh.
            # The startup lock also serializes simultaneous refresh attempts.
            if not stop_if_idle(info, timeout=timeout):
                print("GPU MCP update pending: connected to the existing backend while its jobs or requests finish.",
                      file=sys.stderr)
                return info
        # Retain the reserved port until the child inherits its socket: no free-port race.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        fd = listener.fileno()
        credentials = {"agent_token": secrets.token_urlsafe(32), "human_token": secrets.token_urlsafe(32)}
        reservations.atomic_write_json(root / "credentials.json", credentials)
        command = [sys.executable, str(Path(__file__).with_name("gpu_mcp_backend.py")),
                   "--config", str(config), "--codex-shared", "--runtime-root", str(root),
                   "--listen-fd", str(fd)]
        with (root / "backend.log").open("ab") as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     pass_fds=(fd,), start_new_session=True)
        listener.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = read_info(root)
            if alive(info) and info.get("pid") == child.pid:
                try:
                    request(info, "/health", timeout=1)
                    return info
                except (OSError, ValueError):
                    pass
            if child.poll() is not None:
                raise RuntimeError(f"Shared GPU MCP failed to start. See {root / 'backend.log'}")
            time.sleep(0.05)
        raise TimeoutError(f"Shared GPU MCP startup timed out. See {root / 'backend.log'}")


def relay(config, base=None):
    if not (os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("GPU_MCP_TEST_SKIP_CODEX_VERSION_CHECK") == "1"):
        from gpu_mcp_codex_versions import require_daemon
        require_daemon()
    info = ensure_backend(config, base=base)
    output_lock = threading.Lock()

    def forward(message):
        nonlocal info
        try:
            if not alive(info):
                info = ensure_backend(config, base=base)
            response = request(info, "/mcp", payload=message)
        except Exception as exc:
            # A lost reply may follow a successful launch: never resend a tool call blindly.
            response = None if "id" not in message else {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32000, "message": "Shared GPU MCP connection failed; request outcome "
                           "may be unknown. Inspect this session's jobs. " + str(exc)}}
        if response is not None:
            with output_lock:
                print(json.dumps(response), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("MCP messages must be JSON objects")
            except ValueError as exc:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": str(exc)}}), flush=True)
                continue
            pool.submit(forward, message)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state-base", type=Path)
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--stop-if-idle", action="store_true")
    parser.add_argument("--status", action="store_true",
                        help="report installed and running code without starting or stopping the backend")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(backend_status(args.config, base=args.state_base), indent=2))
        return
    if args.stop_if_idle:
        info = read_info(runtime_root(args.config, base=args.state_base))
        if not alive(info):
            print("No live shared GPU MCP backend for this repository.", file=sys.stderr)
            return
        result = request(info, "/human/stop-if-idle", payload={}, human=True)
        print(result["status"] + (": " + result["reason"] if result.get("reason") else ""))
        return
    if args.monitor:
        from gpu_mcp_codex_monitor import main as monitor
        return monitor(["--config", str(args.config)] +
                       (["--state-base", str(args.state_base)] if args.state_base else []))
    relay(args.config, args.state_base)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"GPU MCP startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
