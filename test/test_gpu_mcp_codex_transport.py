"""Local transport acceptance: automatic sharing, authentication and a human UI.

Starts actual backend processes under pytest-owned temporary directories. No
GPU jobs, SSH or model calls are made by this file.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

import gpu_mcp_reservations as reservations
from gpu_mcp_codex_launcher import alive, ensure_backend, process_start, request
from gpu_mcp_codex_state import JobStore, Origin, runtime_root

pytestmark = pytest.mark.contract
SOURCE = Path(__file__).resolve().parents[1]


def write_repo(root, name="repo"):
    repo = root / name
    repo.mkdir()
    for directory in ("jobs", "results", "logs"):
        (repo / directory).mkdir()
    policy = repo / "gpu-mcp.toml"
    policy.write_text(f"schema_version=1\nrepo_root={str(repo)!r}\nnodes=['unused-fixture']\n"
                      "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\nsync_timeout_sec=2\n")
    return policy


def await_dead(info):
    deadline = time.monotonic() + 8
    while alive(info) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(info), "fixture backend did not shut down"


@pytest.fixture()
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(tmp_path / "registry"))
    monkeypatch.setenv("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", "1")
    # Never connect this local transport fixture to the user's real daemon.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    monkeypatch.setenv("GPU_MCP_TEST_SKIP_CODEX_VERSION_CHECK", "1")
    policy = write_repo(tmp_path)
    base = tmp_path / "backends"
    info = ensure_backend(policy, base=base)
    yield policy, base, info
    if alive(info):
        assert request(info, "/human/stop-if-idle", payload={}, human=True)["status"] == "stopping"
        await_dead(info)


def rpc(info, method, params=None, *, identity=None, rpc_id=1):
    params = dict(params or {})
    if identity is not None:
        params["_meta"] = {"threadId": identity}
    return request(info, "/mcp", payload={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})


def tool(info, name, arguments, identity=None):
    result = rpc(info, "tools/call", {"name": name, "arguments": arguments}, identity=identity)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def record_for(policy, info, origin, *, label):
    record = {"schema_version": 1, "job_id": reservations.generate_job_id(suffix=label),
              "active_attempt_id": reservations.generate_attempt_id(), "repo_root": str(policy.parent),
              "server_instance_id": info["server_instance_id"], "job_kind": "gpu", "job_lifecycle": "succeeded",
              "host": "unused-fixture", "gpu_index": 0, "reservation_key": "unused-fixture.gpu0",
              "active_reservation": False, "next_poll_after": None, "script_path": str(policy.parent / "jobs/task.py"),
              "output_file": str(policy.parent / "logs" / (label + ".log")),
              "outcome": {"terminal_status": "success", "exit_code": 0}}
    Path(record["output_file"]).write_text(label + " private log\n")
    store = JobStore(runtime_root(policy, base=Path(info["state_base"])))
    saved = store.save(record, origin=origin, register=True)
    for event in store.events(origin=origin.thread_id):
        store.delivery(event["event_id"], "accepted")
    return saved


def test_concurrent_startup_is_one_real_backend_and_a_different_repo_is_separate(backend, tmp_path):
    policy, base, info = backend
    with ThreadPoolExecutor(max_workers=5) as callers:
        replies = list(callers.map(lambda _: ensure_backend(policy, base=base), range(5)))
    assert {r["server_instance_id"] for r in replies} == {info["server_instance_id"]}
    assert {r["pid"] for r in replies} == {info["pid"]}
    other_policy = write_repo(tmp_path, "other-repo")
    other = ensure_backend(other_policy, base=base)
    try:
        assert other["pid"] != info["pid"] and other["repo_root"] != info["repo_root"]
    finally:
        request(other, "/human/stop-if-idle", payload={}, human=True)
        await_dead(other)


def test_first_connections_race_to_create_backend_and_one_instance_wins(backend, tmp_path):
    _, base, _ = backend
    policy = write_repo(tmp_path, "simultaneous-new-repo")
    with ThreadPoolExecutor(max_workers=4) as callers:
        connections = list(callers.map(lambda _: ensure_backend(policy, base=base), range(4)))
    info = connections[0]
    try:
        assert len({r["pid"] for r in connections}) == 1
        assert len({r["server_instance_id"] for r in connections}) == 1
    finally:
        request(info, "/human/stop-if-idle", payload={}, human=True)
        await_dead(info)


def test_actual_http_tool_calls_are_scoped_and_human_auth_is_separate(backend):
    policy, base, info = backend
    info = dict(info, state_base=str(base))
    a, b = Origin(str(uuid.uuid4())), Origin(str(uuid.uuid4()))
    one = record_for(policy, info, a, label="alpha")
    two = record_for(policy, info, b, label="beta")
    init = rpc(info, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "fresh-contract", "version": "1"}})
    assert init["result"]["serverInfo"]["name"] == "gpu-cluster"
    assert tool(info, "list_gpu_jobs", {}, a.thread_id)["jobs"][0]["job_id"] == one["job_id"]
    assert tool(info, "list_gpu_jobs", {}, b.thread_id)["jobs"][0]["job_id"] == two["job_id"]
    assert tool(info, "list_gpu_jobs", {})["status"] == "refused"
    refusal = tool(info, "manage_gpu_job", {"action": "logs", "job_id": one["job_id"]}, b.thread_id)
    assert refusal == {"status": "refused", "reason": "job unavailable to this session"}
    assert tool(info, "manage_gpu_job", {"action": "logs", "job_id": one["job_id"]}, a.thread_id)["log_tail"] == "alpha private log\n"
    store = JobStore(runtime_root(policy, base=base))
    before = store.records()
    human = request(info, "/human/snapshot", human=True)
    assert {r["job_id"] for r in human["jobs"]} == {one["job_id"], two["job_id"]}
    assert store.records() == before
    with pytest.raises(urllib.error.HTTPError) as denied:
        request(info, "/human/snapshot")
    assert denied.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as denied:
        request(info, "/mcp", payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, human=True)
    assert denied.value.code == 401


def test_mps_case_tools_and_human_logs_use_the_actual_shared_http_routes(backend):
    policy, base, info = backend
    origin, foreign = Origin(str(uuid.uuid4())), Origin(str(uuid.uuid4()))
    record = record_for(policy, dict(info, state_base=str(base)), origin, label='mps_http')
    case_log = policy.parent / 'logs/mps_case.log'
    case_log.write_text('selected MPS case log\n')
    store = JobStore(runtime_root(policy, base=base))
    record.update(job_kind='mps', job_lifecycle='finished', outcome=None, max_concurrent=2,
                  ready_file=str(policy.parent / '.gpu_mcp_state/mps-ready'),
                  cases=[{'case_id':'alpha','attempt_id':'case-attempt-abc123','state':'succeeded',
                          'process':None,'script_path':str(policy.parent / 'jobs/task.py'),
                          'args':[], 'output_file':str(case_log), 'attempts':[]}])
    store.save(record, origin=origin)
    schemas = rpc(info, 'tools/list')['result']['tools']
    launch = next(t for t in schemas if t['name'] == 'run_python_on_gpu')['inputSchema']
    assert {'mps','runs','max_concurrent'} <= set(launch['properties'])
    assert 'script_path' not in launch.get('required', [])
    for action in ('status','logs','stop','retry'):
        refused = tool(info, 'manage_gpu_job', {'job_id':record['job_id'],'case_id':'alpha','action':action}, foreign.thread_id)
        assert refused == {'status':'refused','reason':'job unavailable to this session'}
    result = tool(info, 'manage_gpu_job', {'job_id':record['job_id'],'case_id':'alpha','action':'logs'}, origin.thread_id)
    assert result['log_tail'] == 'selected MPS case log\n'
    assert 'cases' not in result['saved_job']
    result = tool(info, 'manage_gpu_job', {'job_id':record['job_id'],'case_id':'alpha','action':'status'}, origin.thread_id)
    assert result['case']['state'] == 'succeeded' and result['job_lifecycle'] == 'finished'
    before = store.records()
    human = request(info, f"/human/jobs/{record['job_id']}/logs?case_id=alpha", human=True)
    assert human['tail'] == 'selected MPS case log\n'
    assert store.records() == before
    with pytest.raises(urllib.error.HTTPError) as denied:
        request(info, f"/human/jobs/{record['job_id']}/logs?case_id=alpha")
    assert denied.value.code == 401


def test_stdio_connection_close_keeps_backend_alive_and_job_history(backend):
    policy, base, info = backend
    process = subprocess.Popen([sys.executable, str(SOURCE / "gpu_mcp_codex_launcher.py"),
        "--config", str(policy), "--state-base", str(base)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    payload = {"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
                "capabilities": {}, "clientInfo": {"name": "gateway-fixture", "version": "1"}}}
    out, err = process.communicate(json.dumps(payload) + "\n", timeout=15)
    assert process.returncode == 0, err
    assert json.loads(out)["id"] == 6
    assert alive(info)
    assert ensure_backend(policy, base=base)["pid"] == info["pid"]


def test_monitor_pty_renders_and_escape_leaves_backend_running(backend):
    policy, base, info = backend
    info = dict(info, state_base=str(base))
    for label in ('alpha', 'beta'):
        record_for(policy, info, Origin(str(uuid.uuid4())), label=label)
    before = request(info, '/human/snapshot', human=True)
    selected = before['jobs'][1]
    selected_log = Path(selected['output_file']).stem + ' private log'
    beta_job = next(r['job_id'] for r in before['jobs'] if Path(r['output_file']).stem == 'beta')
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, str(SOURCE / "gpu_mcp_codex_monitor.py"),
        "--config", str(policy), "--state-base", str(base)], stdin=slave, stdout=slave, stderr=slave,
        env=dict(os.environ, TERM="xterm-256color"))
    os.close(slave)
    capture = bytearray()

    def await_text(needle, start=0):
        deadline = time.monotonic() + 5
        while needle not in capture[start:] and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                capture.extend(os.read(master, 65536))
            assert process.poll() is None, capture.decode(errors='replace')
        assert needle in capture[start:], capture[start:].decode(errors='replace')

    try:
        await_text(b'GPU MCP Monitor')
        start = len(capture)
        os.write(master, b'/beta\r\x1bOC')
        # Curses can echo each search character in separate cursor updates.
        # Verify the filtered job in Details rather than the raw input echo.
        await_text(beta_job.encode(), start)
        os.write(master, b'\x1bOD')
        # Search text is handled inside the human viewer, then cleared there.
        os.write(master, b'/\x15\r')
        os.write(master, b'\x1bOB')  # Actual application-mode Down arrow.
        start = len(capture)
        os.write(master, b'\x1bOC')
        await_text(b'"schema_version"', start)
        start = len(capture)
        os.write(master, b'\x1bOC')
        await_text(selected_log.encode(), start)
        start = len(capture)
        os.write(master, b'\x1bOC')
        await_text(b'Events', start)
        start = len(capture)
        os.write(master, b'\x1bOD')
        await_text(selected_log.encode(), start)
        os.write(master, b"\x1b")
        process.wait(timeout=5)
        assert process.returncode == 0
        assert alive(info)
        assert b"\x1b" in capture  # A terminal view, not a plain JSON transcript.
        after = request(info, '/human/snapshot', human=True)
        assert after['jobs'] == before['jobs']
        assert after['events'] == before['events']
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)


def test_monitor_rejects_nonhuman_output_without_starting_backend(tmp_path):
    policy = write_repo(tmp_path)
    base = tmp_path / "states"
    result = subprocess.run([sys.executable, str(SOURCE / "gpu_mcp_codex_monitor.py"),
        "--config", str(policy), "--state-base", str(base)], capture_output=True, text=True)
    assert result.returncode == 2 and "human terminal" in result.stderr
    assert not base.exists()


def test_human_log_reads_are_bounded_and_reject_nonregular_files(tmp_path, monkeypatch):
    import gpu_mcp_codex_host as host
    path = tmp_path / 'large.log'
    with path.open('wb') as log:
        log.seek(10 * 1024 * 1024)
        log.write(b'ordinary output\nGPU_MCP_PROGRESS {"current": 7, "total": 10}\n')
    original = host.os.fdopen
    reads = []

    class File:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def read(self, size):
            assert 0 <= size <= 32000, 'human progress must not scan the complete log'
            reads.append(size)
            return self.inner.read(size)

    monkeypatch.setattr(host.os, 'fdopen', lambda *args, **kwargs: File(original(*args, **kwargs)))
    tail = host.read_human_log_tail(path)
    assert '"current": 7' in tail and len(tail.encode()) <= 32000
    assert reads == [32000]
    fifo = tmp_path / 'pipe.log'
    os.mkfifo(fifo)
    with pytest.raises(OSError, match='regular file'):
        host.read_human_log_tail(fifo)


def test_pid_reuse_or_missing_start_identity_cannot_reuse_backend():
    info = {"hostname": __import__("socket").gethostname(), "pid": os.getpid(), "process_start": None}
    assert not alive(info)
    info["process_start"] = str(int(process_start(os.getpid())) + 1)
    assert not alive(info)


def test_runtime_setting_change_cannot_silently_reuse_an_old_interpreter(backend, monkeypatch):
    policy, base, info = backend
    monkeypatch.setenv("GPU_MCP_PYTHON", "/unavailable-fixture-python")
    with pytest.raises(RuntimeError, match="different runtime settings"):
        ensure_backend(policy, base=base)
    assert alive(info)


def test_human_viewer_reads_existing_backend_without_changing_runtime_settings(backend, monkeypatch):
    policy, base, info = backend
    monkeypatch.setenv("GPU_MCP_PYTHON", "/different-viewer-environment")
    from gpu_mcp_codex_monitor import connect_viewer
    attached = connect_viewer(policy, base=base)
    assert attached["server_instance_id"] == info["server_instance_id"]
    assert attached["runtime_signature"] == info["runtime_signature"]
