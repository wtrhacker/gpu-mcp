from __future__ import annotations

"""Forward contract for safe `kill_gpu_process` behavior.

These tests port the useful parts of the stale hard-coded server tests into the
future config-driven API. They do not require a launch registry for v1; the v1
contract is owner check plus fingerprint confirmation before signaling.

These deterministic tests use `support.kill_policy_contract_helper`.
The real battlefield suite exercises the live MCP tool implemented in
`gpu_mcp_server.py`.
"""

import importlib
import signal
from dataclasses import dataclass

import pytest


pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class KillPolicy:
    nodes: tuple[str, ...] = ("gpu-a",)
    ssh_user: str = "tingran"


@pytest.fixture()
def kill_module():
    return importlib.import_module("support.kill_policy_contract_helper")


def test_kill_inspects_owned_process_without_signal(kill_module):
    calls = []

    def remote_runner(host: str, cmd: str) -> str:
        calls.append((host, cmd))
        if cmd.startswith("ps -p 1234"):
            return (
                "1234 1 1234 tingran Thu May 14 20:11:03 2026 "
                "/usr/bin/python run_job.py --flag value"
            )
        if "nvidia-smi --query-compute-apps" in cmd:
            return "1234, GPU-deadbeef, 4242"
        if "nvidia-smi --query-gpu" in cmd:
            return "0, GPU-deadbeef"
        raise AssertionError(f"unexpected command: {cmd}")

    result = kill_module.inspect_gpu_process(
        KillPolicy(),
        host="gpu-a",
        pid=1234,
        remote_runner=remote_runner,
    )

    assert result["status"] == "inspect"
    assert result["killable"] is True
    assert result["owner"] == "tingran"
    assert result["signal_sent"] is None
    assert result["fingerprint"].startswith("gpu-mcp-kill-v1:")
    assert all("kill -" not in cmd for _, cmd in calls)


def test_kill_refuses_other_user(kill_module):
    def remote_runner(host: str, cmd: str) -> str:
        if cmd.startswith("ps -p 1234"):
            return (
                "1234 1 1234 hdp Thu May 14 20:11:03 2026 "
                "/usr/bin/python someone_else.py"
            )
        if "nvidia-smi" in cmd:
            return ""
        raise AssertionError(f"unexpected command: {cmd}")

    result = kill_module.inspect_gpu_process(
        KillPolicy(),
        host="gpu-a",
        pid=1234,
        remote_runner=remote_runner,
    )

    assert result["status"] == "inspect"
    assert result["killable"] is False
    assert "owner hdp" in result["reason"]


def test_kill_requires_matching_fingerprint(kill_module):
    def remote_runner(host: str, cmd: str) -> str:
        if cmd.startswith("ps -p 1234"):
            return (
                "1234 1 1234 tingran Thu May 14 20:11:03 2026 "
                "/usr/bin/python run_job.py"
            )
        if "nvidia-smi" in cmd:
            return ""
        raise AssertionError(f"unexpected command: {cmd}")

    result = kill_module.kill_gpu_process(
        KillPolicy(),
        host="gpu-a",
        pid=1234,
        fingerprint="wrong",
        remote_runner=remote_runner,
    )

    assert result["status"] == "refused"
    assert "fingerprint mismatch" in result["reason"]


def test_kill_sends_signal_only_after_fingerprint_match(kill_module):
    calls = []

    def remote_runner(host: str, cmd: str) -> str:
        calls.append((host, cmd))
        if cmd.startswith("ps -p 1234"):
            return (
                "1234 1 1234 tingran Thu May 14 20:11:03 2026 "
                "/usr/bin/python run_job.py"
            )
        if "nvidia-smi" in cmd:
            return ""
        if cmd == f"kill -{signal.SIGTERM.value} 1234":
            return ""
        raise AssertionError(f"unexpected command: {cmd}")

    inspected = kill_module.inspect_gpu_process(
        KillPolicy(),
        host="gpu-a",
        pid=1234,
        remote_runner=remote_runner,
    )
    killed = kill_module.kill_gpu_process(
        KillPolicy(),
        host="gpu-a",
        pid=1234,
        fingerprint=inspected["fingerprint"],
        signal="TERM",
        remote_runner=remote_runner,
    )

    assert killed["status"] == "signaled"
    assert killed["signal_sent"] == "TERM"
    assert ("gpu-a", f"kill -{signal.SIGTERM.value} 1234") in calls
