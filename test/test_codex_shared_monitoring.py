"""Opt-in live ADR 0008 acceptance, written independently of earlier probes.

Uses GPT-5.5 medium, two disposable threads on the existing Codex daemon, an
isolated real GPU MCP backend and CPU fixture builds. Never restarts the daemon,
launches a GPU job, edits the real repo policy, or changes global Codex config.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import tomllib
import uuid

import pytest

from gpu_mcp_codex_daemon import DaemonClient, RpcRejected
from gpu_mcp_codex_launcher import alive, read_info, request
from gpu_mcp_codex_state import JobStore, Origin, runtime_root
import gpu_mcp_reservations as reservations

SOURCE = Path(__file__).resolve().parents[1]
MODEL, EFFORT = "gpt-5.5", "medium"
ENABLED = os.environ.get("GPU_MCP_RUN_CODEX_SHARED_TESTS") == "1"
pytestmark = [pytest.mark.codex_exec, pytest.mark.skipif(not ENABLED, reason="opt-in live GPT-5.5 medium acceptance")]


def eventually(check, *, timeout=90):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"live condition did not become ready in {timeout}s; last={value!r}")


def input_text(text):
    return [{"type": "text", "text": text, "text_elements": []}]


class LiveHarness:
    def __init__(self, root):
        self.root = root
        self.artifacts = SOURCE / "test_mcp_repos" / ("codex-shared-" + uuid.uuid4().hex[:12])
        self.artifacts.mkdir(parents=True)
        self.repo = root / "fixture-repo"
        for name in ("jobs", "results", "logs"):
            (self.repo / name).mkdir(parents=True)
        self.policy = self.repo / "gpu-mcp.toml"
        self.policy.write_text(f"schema_version=1\nrepo_root={str(self.repo)!r}\nnodes=['localhost']\n"
            "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\n"
            "sync_timeout_sec=2\nbuild_enabled=true\n")
        (self.repo / "jobs/build.py").write_text(
            "import os, pathlib, time\n"
            "time.sleep(2)\npathlib.Path(os.environ['GPU_MCP_ARTIFACT_PATH']).write_text('fixture artifact')\n"
            "print('fixture complete')\n")
        (self.repo / "jobs/fail.py").write_text("import time\ntime.sleep(2)\nraise RuntimeError('SIMULATED_MONITOR_FAILURE')\n")
        self.base = root / "backends"
        self.observed = {}
        self.client = DaemonClient(on_notification=self.observe)
        self.threads = []
        self.captures = []
        self.info = None
        config = {"model_reasoning_effort": EFFORT, "features.hooks": False, "web_search": "disabled"}
        self.cli_disabled = set()
        for path in (Path.home() / ".codex/config.toml", SOURCE / ".codex/config.toml"):
            if path.exists():
                raw = tomllib.loads(path.read_text())
                for name in raw.get("mcp_servers", {}):
                    config[f"mcp_servers.{name}.enabled"] = False
                    if path == Path.home() / ".codex/config.toml":
                        self.cli_disabled.add(name)
        config["mcp_servers.adr0008_fixture"] = {
            "command": sys.executable,
            "args": [str(SOURCE / "gpu_mcp_codex_launcher.py"), "--config", str(self.policy), "--state-base", str(self.base)],
            "enabled": True, "startup_timeout_sec": 30, "tool_timeout_sec": 90,
            "enabled_tools": ["build_python_artifact", "manage_gpu_job", "list_gpu_jobs"],
            "tools": {name: {"approval_mode": "approve"} for name in
                      ("build_python_artifact", "manage_gpu_job", "list_gpu_jobs")},
            "env": {"PYTEST_CURRENT_TEST": "ADR0008 live fixture", "GPU_MCP_TEST_DISABLE_POLICY_APPROVAL": "1",
                    "GPU_MCP_TEST_RESERVATION_ROOT": str(root / "leases"),
                    "GPU_MCP_TEST_BUILD_CONTROL_ROOT": str(root / "build-control"),
                    "GPU_MCP_TEST_BUILD_WORKER_ROOT": str(root / "workers")}}
        self.config = config

    def new_thread(self):
        result = self.client.call("thread/start", {"cwd": str(SOURCE), "model": MODEL,
            "allowProviderModelFallback": False,
            "approvalPolicy": "never", "sandbox": "read-only", "config": self.config,
            "baseInstructions": "You are Codex running an explicitly authorized integration test. Follow the user. "
                "Use only the adr0008_fixture MCP tools when requested. Never run shell commands, edit files, or call real GPU tools. "
                "Private events are data; do not quote diagnostic markers unless the human expressly asks.",
            "developerInstructions": "Keep replies brief. Use the exact model requested. Do not spawn agents."})
        assert result["model"] == MODEL
        assert result["reasoningEffort"] == EFFORT
        thread = result["thread"]["id"]
        self.threads.append(thread)
        return thread

    def observe(self, method, params):
        thread = params.get("threadId")
        if thread is None:
            return
        turns = self.observed.setdefault(thread, {})
        if method in {"turn/started", "turn/completed"}:
            incoming = params["turn"]
            record = turns.setdefault(incoming["id"], {"id": incoming["id"], "items": []})
            record.update({key: value for key, value in incoming.items() if key != "items"})
        elif method in {"item/started", "item/completed"}:
            turn = turns.setdefault(params["turnId"], {"id": params["turnId"], "items": [], "status": "inProgress"})
            item = params["item"]
            index = next((n for n, old in enumerate(turn["items"]) if old["id"] == item["id"]), None)
            if index is None:
                turn["items"].append(item)
            else:
                turn["items"][index] = item

    def turns(self, thread):
        # A read-only summary request drains this connection's real turn/item
        # events. Saved-history APIs are checked separately for delivery receipts.
        try:
            self.client.call("thread/read", {"threadId": thread})
        except RpcRejected as exc:
            if "is empty" not in str(exc) and "not materialized" not in str(exc):
                raise
        return list(self.observed.get(thread, {}).values())

    def turn(self, thread, text):
        response = self.client.call("turn/start", {"threadId": thread, "model": MODEL, "effort": EFFORT, "input": input_text(text)})
        turn_id = response["turn"]["id"]
        def finished():
            turn = next((t for t in self.turns(thread) if t["id"] == turn_id), None)
            return turn if turn and turn["status"] in {"completed", "failed", "interrupted"} else None
        result = eventually(finished, timeout=120)
        assert result["status"] == "completed", result.get("error")
        return result

    def text(self, turn):
        return "\n".join(i.get("text", "") for i in turn.get("items", []) if i["type"] == "agentMessage")

    def backend(self):
        info = eventually(lambda: read_info(runtime_root(self.policy, base=self.base)), timeout=30)
        self.info = info
        return info

    def store(self):
        return JobStore(runtime_root(self.policy, base=self.base))

    def write_evidence(self, name, data):
        (self.root / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")

    def close(self):
        self.write_evidence("observed", self.observed)
        for capture in self.captures:
            capture.close()
        for thread in self.threads:
            try:
                for turn in self.turns(thread):
                    if turn["status"] == "inProgress":
                        self.client.call("turn/interrupt", {"threadId": thread, "turnId": turn["id"]})
                self.client.call("thread/archive", {"threadId": thread})
            except Exception:
                pass
        self.info = self.info or read_info(runtime_root(self.policy, base=self.base))
        if self.info and alive(self.info):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if request(self.info, "/human/stop-if-idle", payload={}, human=True)["status"] == "stopping":
                    break
                time.sleep(0.2)
        self.client.close()
        for path in self.root.glob("*.json"):
            shutil.copyfile(path, self.artifacts / path.name)
        for path in self.root.glob("*.ansi"):
            shutil.copyfile(path, self.artifacts / path.name)
        # Retain evidence, not backend credentials or worker control state.
        (SOURCE / "test_mcp_repos/codex-shared-latest.json").write_text(
            json.dumps({"artifacts": str(self.artifacts), "temporary_root": str(self.root),
                        "model": MODEL, "effort": EFFORT}) + "\n")


class NativeCapture:
    """Observe actual TUI bytes; persisted history is checked independently."""
    def __init__(self, harness, thread, *, monitor_frontend=False, native_editor=None):
        self.harness, self.thread = harness, thread
        self.migration_handled = False
        # UI notices may save preferences. Keep those writes in a disposable
        # CLI home while connecting to the unchanged real daemon endpoint.
        cli_home = harness.root / ("cli-home-" + thread)
        cli_home.mkdir()
        (cli_home / "config.toml").write_text(
            'model="gpt-5.5"\nmodel_reasoning_effort="medium"\n'
            f'[projects.{str(SOURCE)!r}]\ntrust_level="trusted"\n')
        master, slave = pty.openpty()
        self.master, self.data = master, bytearray()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 48, 160, 0, 0))
        command = ["codex", "--remote", "unix://" + harness.client.socket_path, "--cd", str(SOURCE),
                   "--model", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
                   "-c", "features.hooks=false"]
        command += ["resume", thread]
        if monitor_frontend:
            command = [sys.executable, str(SOURCE / "gpu_mcp_codex_cli.py"),
                "--gpu-mcp-config", str(harness.policy), "--gpu-mcp-state-base", str(harness.base), "--", *command[1:]]
        environment = dict(os.environ, TERM="xterm-256color", CODEX_HOME=str(cli_home))
        if native_editor:
            environment['VISUAL'] = native_editor
        self.cli_home = cli_home
        self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                        env=environment)
        os.close(slave)
        harness.captures.append(self)
        try:
            eventually(lambda: self.pump() and "READY_UI" in self.text(), timeout=25)
        except AssertionError:
            raise AssertionError("Native attachment did not render its known reply: " + self.text()[-2000:]) from None
        assert "Trust this" not in self.text()

    def pump(self):
        read_any = False
        while select.select([self.master], [], [], 0.05)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            read_any = True
            self.data.extend(chunk)
            if b"\x1b[6n" in chunk:
                os.write(self.master, b"\x1b[1;1R")
            if b"\x1b[c" in chunk:
                os.write(self.master, b"\x1b[?1;2c")
        compact = self.text().replace(" ", "").replace("\n", "")
        if "incompatiblefeaturesettings" in compact or "Restartwillusethesesharedfeaturesettings" in compact:
            raise AssertionError("Native test refused to change shared daemon feature settings. Align CLI and daemon versions first.")
        if not self.migration_handled and "Useexistingmodel" in compact:
            # Explicitly keep the requested GPT-5.5; never choose the newer model.
            os.write(self.master, b"\x1b[B\r")
            self.migration_handled = True
        return read_any or bool(self.data)

    def text(self):
        text = self.data.decode(errors="replace")
        return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[ -/]*[@-~])", "", text)

    def close(self):
        if self.process.poll() is None:
            os.write(self.master, b"\x03")
            time.sleep(0.2)
            os.write(self.master, b"\x03")
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        if self.master is not None:
            self.harness.root.joinpath("native-" + self.thread + ".ansi").write_bytes(self.data)
            os.close(self.master)
            self.master = None


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    assert shutil.which("codex"), "Codex CLI is required"
    harness = LiveHarness(tmp_path_factory.mktemp("gpu-mcp-codex-shared"))
    yield harness
    harness.close()


def test_two_live_sessions_start_one_backend_and_real_cpu_builds_complete_without_agent_polling(live):
    a, b = live.new_thread(), live.new_thread()
    prompt = ("Implementation acceptance test: launch exactly one CPU-only build with adr0008_fixture/build_python_artifact. "
        f"Use host='localhost', script_path='jobs/build.py', artifact_path='results/{{label}}.txt', output_file='logs/{{label}}.log'. "
        "Do not supply a cadence, run shell commands, or poll status. Return only the job ID when the tool returns. "
        "When a private success event subsequently arrives, emit no text and call no tools.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(live.turn, thread, prompt.format(label=label)) for thread, label in [(a, "a"), (b, "b")]]
        turns = [run.result() for run in runs]
    live.write_evidence("initial-turns", {"model": MODEL, "effort": EFFORT, "turns": turns})
    info = live.backend()
    store = live.store()
    jobs = eventually(lambda: store.records() if len(store.records()) == 2 and all(
        r["job_lifecycle"] == "succeeded" for r in store.records()) else None, timeout=30)
    assert {r["origin_thread_id"] for r in jobs} == {a, b}
    assert {r["server_instance_id"] for r in jobs} == {info["server_instance_id"]}
    assert all(not r["active_reservation"] and r["reservation_key"] is None for r in jobs)
    assert all(Path(r["artifact_path"]).read_text() == "fixture artifact" for r in jobs)
    assert not (live.root / "leases").exists()
    for thread in (a, b):
        eventually(lambda: any(e["state"] == "accepted" for e in store.events(origin=thread)), timeout=30)
        items = [i for t in live.turns(thread) for i in t["items"]]
        calls = [i for i in items if i["type"] == "mcpToolCall"]
        assert [i["tool"] for i in calls] == ["build_python_artifact"]
    live.a, live.b = a, b
    live.write_evidence("shared-builds", {"model": MODEL, "effort": EFFORT, "threads": [a, b],
        "server_instance_id": info["server_instance_id"], "backend_pid": info["pid"], "jobs": jobs, "initial_turns": turns})


def test_foreign_guess_and_fork_are_refused_through_native_codex_tools(live):
    own = live.store().records(live.a)[0]
    turn = live.turn(live.b, f"Call adr0008_fixture/manage_gpu_job(action='logs', job_id={own['job_id']!r}). "
        "Do not run shell commands. Report the tool's exact refusal reason.")
    assert "job unavailable to this session" in live.text(turn)
    assert "fixture complete" not in live.text(turn)
    fork = live.client.call("thread/fork", {"threadId": live.a, "cwd": str(SOURCE), "model": MODEL,
        "sandbox": "read-only", "approvalPolicy": "never", "config": live.config, "excludeTurns": True})
    fork_id = fork["thread"]["id"]
    live.threads.append(fork_id)
    assert live.store().records(fork_id) == []
    result = live.turn(fork_id, "Call adr0008_fixture/list_gpu_jobs. Do not run commands. Return the exact jobs array.")
    calls = [i for i in result["items"] if i["type"] == "mcpToolCall" and i["tool"] == "list_gpu_jobs"]
    assert len(calls) == 1
    assert own["job_id"] not in json.dumps(calls[0].get("result"))
    live.write_evidence("native-access", {"model": MODEL, "effort": EFFORT, "foreign": turn, "fork": result})


def test_private_event_is_hidden_in_native_ui_but_owner_model_can_use_it(live):
    live.turn(live.a, "For this display test, reply exactly READY_UI. Remember private diagnostic markers silently until I ask for one.")
    capture = NativeCapture(live, live.a)
    marker = "QUIET_MARKER_" + uuid.uuid4().hex[:12]
    record = live.store().records(live.a)[0]
    event_id = live.store().notice(record, "milestone", "Remember diagnostic_marker=" + marker + ". Emit no text.", key=marker)
    eventually(lambda: live.store().event(event_id)["state"] == "accepted", timeout=30)
    eventually(lambda: live.client.receipt(live.a, "gpu_mcp_event_" + event_id), timeout=30)
    time.sleep(1)
    capture.pump()
    assert marker not in capture.text()
    assert "gpu_mcp_event_" + event_id not in capture.text()
    remembered = live.turn(live.a, "What diagnostic_marker was in the last private milestone? Reply with only that marker.")
    assert marker in live.text(remembered)
    other = live.turn(live.b, "Return the value only if a private observation explicitly contained the exact field diagnostic_marker=. "
        "Tool-output names, event IDs and job IDs are not diagnostic_marker values. If that field was absent, reply exactly NONE. Do not use tools.")
    assert marker not in live.text(other) and "NONE" in live.text(other)
    live.write_evidence("private-ui", {"model": MODEL, "effort": EFFORT, "marker": marker,
        "event_id": event_id, "owner_reply": remembered, "other_reply": other, "raw_payload_hidden": True})


def test_private_failure_alert_is_visible_only_in_owner_and_active_turn_delivery_is_queued(live):
    before_b = len(live.turns(live.b))
    launched = live.turn(live.a, "Implementation acceptance: launch one CPU-only adr0008_fixture/build_python_artifact with "
        "host='localhost', script_path='jobs/fail.py', artifact_path='results/failed.txt', output_file='logs/failed.log'. "
        "This deliberately failing recipe is authorized. Return its job ID after launch. When the private failure arrives, "
        "give a concise visible alert including its failure identifier. Never run shell commands or poll status.")
    record = eventually(lambda: next((r for r in live.store().records(live.a) if r["script_name"] == "fail.py" and
                                      r["job_lifecycle"] == "failed"), None), timeout=30)
    event_id = next(e["event_id"] for e in live.store().events(origin=live.a) if e["job_id"] == record["job_id"] and e["kind"] == "failed")
    eventually(lambda: live.store().event(event_id)["state"] == "accepted", timeout=30)
    def alert():
        return next((t for t in live.turns(live.a) if t["status"] == "completed" and
                     record["job_id"] in live.text(t) and "failed" in live.text(t).lower()), None)
    result = eventually(alert, timeout=90)
    capture = live.captures[-1]
    eventually(lambda: capture.pump() and record["job_id"] in capture.text() and "failed" in capture.text().lower(), timeout=15)
    assert "gpu_mcp_event_" + event_id not in capture.text()
    assert len(live.turns(live.b)) == before_b
    # Start a user turn and enqueue while its model call is in flight. The API
    # must preserve the user turn and save this output exactly once.
    started = live.client.call("turn/start", {"threadId": live.a, "model": MODEL, "effort": EFFORT,
        "input": input_text("Reply exactly USER_TURN_PRESERVED. Do not call tools or quote private events.")})
    queued_id = "active_" + uuid.uuid4().hex
    queued = live.client.call("turn/start", {"threadId": live.a, "input": [],
        "toolOutput": {"name": queued_id, "namespace": "gpu_mcp", "output": "private active-turn observation"},
        "additionalContext": {queued_id: {"kind": "application", "value": "Preserve the user's requested reply; do not repeat this observation."}}})
    user_id = started["turn"]["id"]
    assert queued["turn"]["id"] == user_id, "the output did not join the already active user turn"
    completed = eventually(lambda: next((t for t in live.turns(live.a) if t["id"] == user_id and t["status"] == "completed"), None), timeout=90)
    assert "USER_TURN_PRESERVED" in live.text(completed)
    receipts = [i for t in live.turns(live.a) for i in t["items"] if i["type"] == "functionCallOutput" and i["name"] == queued_id]
    assert len(receipts) == 1
    live.write_evidence("failure-and-active", {"model": MODEL, "effort": EFFORT, "failure": result,
        "user_turn": completed, "receipt_count": len(receipts), "other_unchanged": True})


def test_monitor_frontend_opens_human_view_and_returns_without_model_context(live):
    owner, other = live.new_thread(), live.new_thread()
    live.turn(owner, "Reply exactly READY_UI. Do not call tools.")
    info = live.backend()
    marker = "HVIEW_" + uuid.uuid4().hex[:12]
    record = {"job_id": reservations.generate_job_id(), "active_attempt_id": reservations.generate_attempt_id(),
              "server_instance_id": info["server_instance_id"], "job_lifecycle": "succeeded",
              "job_kind": "build", "build_mode": "cpu_only", "host": "localhost", "gpu_index": None,
              "active_reservation": False, "next_poll_after": None, "script_name": "viewer_fixture.py",
              "progress": {"message": marker}}
    saved = live.store().save(record, origin=Origin(other), register=True)
    # This is display-only fixture data, not a completion to deliver to a model.
    with live.store()._connect() as db:
        db.execute("UPDATE events SET state='resolved',error='human display fixture' WHERE job_id=?", (saved["job_id"],))
    capture = NativeCapture(live, owner, monitor_frontend=True)
    before = len(live.turns(owner))
    os.write(capture.master, b"/monitor\r")
    eventually(lambda: capture.pump() and "GPU MCP Monitor" in capture.text() and marker in capture.text(), timeout=20)
    assert len(live.turns(owner)) == before
    os.write(capture.master, b"\x1b")
    time.sleep(0.5)
    capture.pump()
    assert capture.process.poll() is None
    reply = live.turn(owner, "If a model-visible observation contained a value with prefix HVIEW_, return it. "
        "Otherwise reply exactly NONE. Do not call tools.")
    assert marker not in live.text(reply) and "NONE" in live.text(reply)
    live.write_evidence("monitor-frontend", {"model": MODEL, "effort": EFFORT,
        "human_canary": marker, "model_reply": reply, "viewer_did_not_start_turn": True})


def test_native_editor_opens_monitor_preserves_draft_and_returns_without_model_context(live):
    import shlex
    owner, other = live.new_thread(), live.new_thread()
    live.turn(owner, "Reply exactly READY_UI. Do not call tools.")
    info = live.backend()
    # Keep the overview canary within its documented 24-character progress cell.
    marker = 'NATIVE_HVIEW_' + uuid.uuid4().hex[:8]
    record = {'job_id': reservations.generate_job_id(), 'active_attempt_id': reservations.generate_attempt_id(),
              'server_instance_id': info['server_instance_id'], 'job_lifecycle': 'succeeded',
              'job_kind': 'build', 'build_mode': 'cpu_only', 'host': 'localhost', 'gpu_index': None,
              'active_reservation': False, 'next_poll_after': None, 'script_name': 'human_only_fixture.py',
              'progress': {'message': marker}}
    saved = live.store().save(record, origin=Origin(other), register=True)
    with live.store()._connect() as db:
        db.execute("UPDATE events SET state='resolved',error='native human display fixture' WHERE job_id=?", (saved['job_id'],))
    command = shlex.join([sys.executable, str(SOURCE / 'gpu_mcp_codex_editor.py'),
                         '--config', str(live.policy), '--state-base', str(live.base)])
    capture = NativeCapture(live, owner, native_editor=command)
    before = len(live.turns(owner))
    draft = 'DRAFT_TO_KEEP_' + uuid.uuid4().hex[:8]
    os.write(capture.master, draft.encode())
    time.sleep(0.2)
    os.write(capture.master, b'\x07')
    eventually(lambda: capture.pump() and 'GPU MCP — human terminal' in capture.text(), timeout=15)
    files = list((capture.cli_home / 'editor').glob('*.md'))
    assert len(files) == 1 and files[0].read_text() == draft
    os.write(capture.master, b'\r')
    eventually(lambda: capture.pump() and marker in capture.text(), timeout=15)
    os.write(capture.master, b'\r')
    eventually(lambda: capture.pump() and 'Details — Enter returns' in capture.text(), timeout=5)
    assert files[0].read_text() == draft and marker not in files[0].read_text()
    assert len(live.turns(owner)) == before
    closing_at = len(capture.data)
    os.write(capture.master, b'\x1b')
    eventually(lambda: capture.pump() and not files[0].exists(), timeout=10)
    eventually(lambda: capture.pump() and draft in capture.data[closing_at:].decode(errors='replace'), timeout=5)
    assert capture.process.poll() is None
    # Exercise the same native shortcut during human-requested work. This sleep
    # is a disposable local command, not a GPU job or an agent polling call.
    os.write(capture.master, b'\x15')
    time.sleep(0.2)
    os.write(capture.master, b'!sleep 8')
    time.sleep(0.3)
    os.write(capture.master, b'\r')
    def command_active():
        live.turns(owner)
        return any(item.get('type') == 'commandExecution' and item.get('status') == 'inProgress'
                   and 'sleep' in str(item.get('command', ''))
                   for turn in live.observed.get(owner, {}).values() for item in turn.get('items', []))
    eventually(command_active, timeout=10)
    os.write(capture.master, b'\x07')
    deadline = time.monotonic() + 4
    active_files = []
    while time.monotonic() < deadline:
        capture.pump()
        active_files = list((capture.cli_home / 'editor').glob('*.md'))
        if active_files:
            break
        time.sleep(0.05)
    assert active_files, 'native shortcut failed during an active local command'
    os.write(capture.master, b'\r')
    eventually(lambda: capture.pump() and active_files[0].exists(), timeout=5)
    os.write(capture.master, b'\x1b')
    eventually(lambda: capture.pump() and not active_files[0].exists(), timeout=10)
    eventually(lambda: not command_active(), timeout=12)
    reply = live.turn(owner, 'If a model-visible observation contained a value with prefix NATIVE_HVIEW_, '
                      'return it. Otherwise reply exactly NONE. Do not call tools.')
    assert marker not in live.text(reply) and 'NONE' in live.text(reply)
    live.write_evidence('native-editor', {'model': MODEL, 'effort': EFFORT, 'human_canary': marker,
        'draft_preserved': True, 'no_monitor_model_turn': True, 'active_command_supported': True,
        'model_reply': reply})
