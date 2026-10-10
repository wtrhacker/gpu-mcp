"""Human /monitor interception and actual terminal ownership boundaries."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

import pytest

from gpu_mcp_codex_cli import MonitorInput, interactive, options, working_directory

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[1]


def transmitted(actions):
    return b"".join(value for kind, value in actions if kind == "send")


def test_typed_monitor_is_consumed_before_codex_receives_it():
    command = MonitorInput()
    actions = []
    for part in (b"/m", b"oni", b"tor", b"\r"):
        actions += command.feed(part)
    assert actions == [("monitor", None)]


def test_other_slash_commands_and_normal_prompts_are_unchanged():
    for text in (b"/model\r", b"explain /monitor to me\r", b"/monitoring\r"):
        actions = MonitorInput().feed(text)
        assert transmitted(actions) == text
        assert not any(kind == "monitor" for kind, _ in actions)


def test_pasted_monitor_is_literal_prompt_text_and_does_not_open_viewer():
    text = b"\x1b[200~/monitor\nordinary text\x1b[201~\r"
    actions = MonitorInput().feed(text)
    assert transmitted(actions) == text
    assert not any(kind == "monitor" for kind, _ in actions)


def test_pending_typo_can_be_corrected_without_leaking_command_to_codex():
    commands = MonitorInput()
    actions = commands.feed(b"/moni\x7ftor\r")
    assert transmitted(actions) == b"/montor\r"  # Not the exact command.
    commands = MonitorInput()
    assert commands.feed(b"/monit\x7ftor\r") == [("monitor", None)]


def test_terminal_cursor_report_does_not_disable_local_command():
    parser = MonitorInput()
    report = b"\x1b[1;1R"
    assert transmitted(parser.feed(report)) == report
    assert parser.feed(b"/monitor\r") == [("monitor", None)]


def test_remote_args_cd_resume_and_noninteractive_commands_are_preserved():
    own, args = options(["--gpu-mcp-config", "/repo/gpu-mcp.toml", "--", "--cd", "/repo/sub", "resume", "session"])
    assert own == {"--gpu-mcp-config": "/repo/gpu-mcp.toml"}
    assert args == ["--cd", "/repo/sub", "resume", "session"]
    assert working_directory(args) == Path("/repo/sub")
    assert interactive(args)
    assert not interactive(["app-server", "daemon", "version"])
    assert not interactive(["--model", "gpt-5.5", "exec", "do something"])
    assert not interactive(["--version"])


def test_actual_pty_viewer_never_sends_monitor_data_to_child_and_child_survives(tmp_path):
    child_log = tmp_path / "codex-input.bin"
    child = tmp_path / "child.py"
    child.write_text(
        "import os,signal,tty\n"
        "tty.setraw(0)\n"
        "signal.signal(signal.SIGWINCH,lambda *args:os.write(1,b'CODEX_SCREEN_READY\\r\\n'))\n"
        "os.write(1,b'CODEX_SCREEN_READY\\r\\n')\n"
        "while True:\n"
        " data=os.read(0,4096)\n"
        " if not data:break\n"
        f" with open({str(child_log)!r},'ab') as record:record.write(data)\n"
        " os.write(1,b'CHILD_STILL_ALIVE\\r\\n')\n"
        " if b'\\x04' in data:break\n")
    driver = tmp_path / "driver.py"
    driver.write_text(
        f"import sys,os\nsys.path.insert(0,{str(ROOT)!r})\n"
        "from gpu_mcp_codex_cli import frontend\n"
        "def viewer():\n"
        " os.write(1,b'\\x1b[2J\\x1b[HHUMAN_MONITOR_SECRET\\r\\n')\n"
        " while os.read(0,1)!=b'\\x1b':pass\n"
        f"raise SystemExit(frontend([{sys.executable!r},{str(child)!r}],viewer))\n")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
    process = subprocess.Popen([sys.executable, str(driver)], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    capture = bytearray()

    def wait_for(value):
        deadline = time.monotonic() + 8
        while value not in capture and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    capture.extend(os.read(master, 65536))
                except OSError:
                    break
        assert value in capture, capture.decode(errors="replace")

    try:
        wait_for(b"CODEX_SCREEN_READY")
        os.write(master, b"/monitor\r")
        wait_for(b"HUMAN_MONITOR_SECRET")
        assert process.poll() is None
        os.write(master, b"\x1b")
        os.write(master, b"ordinary task\r")
        wait_for(b"CHILD_STILL_ALIVE")
        os.write(master, b"\x04")
        process.wait(timeout=5)
        assert process.returncode == 0
        data = child_log.read_bytes()
        assert b"ordinary task" in data
        assert b"/monitor" not in data and b"HUMAN_MONITOR_SECRET" not in data
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)
