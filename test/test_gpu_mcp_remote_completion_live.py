"""Opt-in SSH completion checks with the real guarded runner and supervisor.

The child only sleeps and exits. Lease bookkeeping is isolated from production;
no CUDA work or Codex turn is launched. The controller's result lookup stays
unavailable throughout, while process proof and execution-host reads use SSH.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

import gpu_mcp_reservations as leases
from gpu_mcp_codex_runtime import CodexRuntime
from gpu_mcp_codex_state import Origin, UNCERTAIN_STATES
from test_gpu_mcp_codex_shared import QuietDaemon

SOURCE = Path(__file__).resolve().parents[1]
HOSTS = [host.strip() for host in os.environ.get("GPU_MCP_LIVE_OUTCOME_HOSTS", "").split(",") if host.strip()]
pytestmark = [pytest.mark.real_battlefield,
              pytest.mark.skipif(not HOSTS, reason="set GPU_MCP_LIVE_OUTCOME_HOSTS to approved SSH hosts")]


@pytest.mark.parametrize("host", HOSTS or [None])
@pytest.mark.parametrize("code", [0, 17])
@pytest.mark.parametrize("lost_first_read", [False, True], ids=["direct-result", "lost-first-reply"])
def test_remote_completion_survives_an_unavailable_controller_view(tmp_path, monkeypatch, host, code, lost_first_read):
    repo = SOURCE / "test_mcp_repos" / ("completion-check-" + uuid.uuid4().hex)
    for folder in ("jobs", "results", "logs"):
        (repo / folder).mkdir(parents=True)
    (repo / "jobs/check.py").write_text(
        "import time\ntime.sleep(3)\nprint('remote completion check')\n"
        f"raise SystemExit({code})\n")
    config = repo / "gpu-mcp.toml"
    config.write_text(f"schema_version = 1\nrepo_root = {str(repo)!r}\nnodes = [{host!r}]\n"
                      "script_roots = ['jobs']\nwrite_roots = ['results']\noutput_roots = ['logs']\n"
                      "min_free_memory_mib = 0\nsync_timeout_sec = 10\n")
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "leases"))
    monkeypatch.setenv("GPU_MCP_PYTHON", sys.executable)
    monkeypatch.setattr(sys, "argv", ["gpu_mcp_server.py", "--config", str(config)])
    name = "remote_completion_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, SOURCE / "gpu_mcp_server.py")
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, server)
    spec.loader.exec_module(server)
    daemon = QuietDaemon()
    runtime = CodexRuntime(server, tmp_path / "authority", daemon=daemon)
    server.CODEX_RUNTIME = runtime
    owner = Origin(str(uuid.uuid4()))
    monkeypatch.setattr(server.mcp, "get_context", lambda: SimpleNamespace(
        request_context=SimpleNamespace(meta={"threadId": owner.thread_id})))
    reads = []
    actual_remote_read = server._read_execution_host_outcome

    def remote_read(record):
        value = actual_remote_read(record)
        reads.append({"valid_result": value is not None, "observed_at": leases.iso_timestamp()})
        return None if lost_first_read and len(reads) == 1 else value

    monkeypatch.setattr(server, "_read_execution_host_outcome", remote_read)
    monkeypatch.setattr(server, "_read_outcome_record", lambda record: None)
    # No successful control-side result read is permitted, even after completion.
    actual_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path.name == "outcome.json":
            raise FileNotFoundError("injected unavailable controller directory entry")
        return actual_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    try:
        launched = json.loads(server.run_python_on_gpu(
            host=host, gpu_index=0, script_path="jobs/check.py", job_role="one_off",
            cadence_hint_sec=1, job_title="Remote result visibility check"))
        assert launched["status"] in {"launched", "launched_with_warning"}, launched
        job_id = launched["job_id"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            runtime.monitor_once()
            saved = runtime.store.get(job_id)
            if lost_first_read and saved.get("job_lifecycle") in UNCERTAIN_STATES and reads:
                # Let delivery run before the monitor's next retry. Only this
                # timeout is simulated; both underlying host reads are real.
                future = datetime.now(timezone.utc) + timedelta(seconds=15)

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return future.astimezone(tz or timezone.utc)

                monkeypatch.setattr(server, "datetime", Clock)
            runtime.deliver_once()
            runtime.deliver_once()
            saved = runtime.store.get(job_id)
            if saved.get("job_lifecycle") in {"succeeded", "failed"} and not saved["active_reservation"]:
                break
            time.sleep(0.2)
        expected = "succeeded" if code == 0 else "failed"
        assert saved["job_lifecycle"] == expected, saved
        assert saved["active_attempt_id"] == launched["attempt_id"], "completion must not need a retry"
        assert saved["active_reservation"] is False
        assert saved["reason"] == ""
        assert saved["last_process_observation"]["status"] == "gone"
        assert saved["outcome"]["remote_pid"] == saved["remote_pid"]
        assert saved["outcome"]["exit_code"] == code
        assert all(read["valid_result"] for read in reads), "the actual producer read failed"
        assert len(reads) == (2 if lost_first_read else 1)
        assert not any(event["kind"] in UNCERTAIN_STATES for event in daemon.sent)
        assert [event["kind"] for event in daemon.sent if event["kind"] != "poll"] == [expected]
        assert all(event["origin"] == owner.thread_id for event in daemon.sent)
        assert not leases.reservation_dir(leases.reservation_registry_root(), saved["reservation_key"]).exists()
        evidence = {"host": host, "exit_code": code, "lost_first_read": lost_first_read,
                    "controller_result_read": "always unavailable", "producer_reads": reads,
                    "job": saved, "events": runtime.store.events(), "delivered_kinds": [e["kind"] for e in daemon.sent]}
        (repo / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps({"host": host, "lifecycle": expected, "producer_reads": len(reads), "evidence": str(repo)}))
    finally:
        runtime.stop()
        server.HEARTBEAT_MANAGER.stop()
