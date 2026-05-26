from __future__ import annotations

"""Test-support implementation for kill-process policy contracts.

The live MCP server implements the real `kill_gpu_process` tool in
`gpu_mcp_server.py`. This helper is used only by deterministic unit tests so
they can verify the intended safety rules without SSH, GPUs, or real processes.
"""

import hashlib
import signal as signal_module
from typing import Callable


class KillPolicyError(ValueError):
    """Raised for invalid kill policy requests."""


def _nodes(policy) -> tuple[str, ...]:
    return tuple(getattr(policy, "nodes", ()))


def _ssh_user(policy) -> str:
    return str(getattr(policy, "ssh_user", ""))


def _parse_ps(output: str) -> dict:
    parts = output.strip().split(maxsplit=6)
    if len(parts) < 7:
        raise KillPolicyError("could not inspect process")
    return {
        "pid": parts[0],
        "ppid": parts[1],
        "pgid": parts[2],
        "owner": parts[3],
        "start": " ".join(parts[4:6]),
        "command": parts[6],
    }


def _fingerprint(host: str, info: dict) -> str:
    payload = "|".join(
        [
            host,
            info.get("pid", ""),
            info.get("owner", ""),
            info.get("start", ""),
            info.get("command", ""),
        ]
    )
    return "gpu-mcp-kill-v1:" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def inspect_gpu_process(
    policy,
    host: str,
    pid: int,
    remote_runner: Callable[[str, str], str],
) -> dict:
    if host not in _nodes(policy):
        raise KillPolicyError(f"host not allowed: {host}")
    ps_output = remote_runner(host, f"ps -p {pid} -o pid=,ppid=,pgid=,user=,lstart=,args=")
    info = _parse_ps(ps_output)
    expected_owner = _ssh_user(policy)
    killable = info["owner"] == expected_owner

    gpu_index = ""
    gpu_memory_mib = ""
    try:
        apps = remote_runner(host, "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits")
        gpus = remote_runner(host, "nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits")
        if apps.strip() and gpus.strip():
            app_pid, uuid, mem = [part.strip() for part in apps.splitlines()[0].split(",")]
            if app_pid == str(pid):
                gpu_memory_mib = mem
                for line in gpus.splitlines():
                    index, gpu_uuid = [part.strip() for part in line.split(",")]
                    if gpu_uuid == uuid:
                        gpu_index = index
                        break
    except Exception:
        pass

    result = {
        "status": "inspect",
        "host": host,
        "pid": int(info["pid"]),
        "owner": info["owner"],
        "command": info["command"],
        "killable": killable,
        "signal_sent": None,
        "fingerprint": _fingerprint(host, info),
        "gpu_index": gpu_index,
        "gpu_memory_mib": gpu_memory_mib,
    }
    if not killable:
        result["reason"] = f"owner {info['owner']} does not match GPU_MCP_USER {expected_owner}"
    return result


def kill_gpu_process(
    policy,
    host: str,
    pid: int,
    *,
    fingerprint: str | None = None,
    signal: str = "TERM",
    remote_runner: Callable[[str, str], str],
) -> dict:
    inspected = inspect_gpu_process(policy, host, pid, remote_runner)
    if not inspected["killable"]:
        inspected["status"] = "refused"
        return inspected
    if fingerprint != inspected["fingerprint"]:
        return {
            **inspected,
            "status": "refused",
            "reason": "fingerprint mismatch",
        }
    signum = getattr(signal_module, f"SIG{signal}").value
    remote_runner(host, f"kill -{signum} {pid}")
    return {
        **inspected,
        "status": "signaled",
        "signal_sent": signal,
    }
