#!/usr/bin/env python3
"""Thin Codex terminal frontend adding a human-only /monitor command.

The real Codex CLI still owns the conversation. This process intercepts only a
typed /monitor in an empty composer; no viewer data travels through its PTY.
"""
from __future__ import annotations

from collections import deque
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import threading
import tty


class MonitorInput:
    """Conservative command interception; pastes and uncertain edits pass through."""
    command = b"/monitor"

    def __init__(self):
        self.pending = bytearray()
        self.empty = True
        self.paste = False
        self.escape = bytearray()
        self.before_escape = True

    def feed(self, data):
        actions, output = [], bytearray()

        def flush():
            if output:
                actions.append(("send", bytes(output)))
                output.clear()

        for value in data:
            if self.escape:
                output.append(value)
                self.escape.append(value)
                sequence = bytes(self.escape)
                finished = (len(sequence) == 2 and value not in (ord("["), ord("O"))) or (
                    len(sequence) > 2 and 0x40 <= value <= 0x7E) or len(sequence) > 24
                if finished:
                    if sequence == b"\x1b[200~":
                        self.paste = True
                    elif sequence == b"\x1b[201~":
                        self.paste = False
                    elif re.fullmatch(rb"\x1b\[\d+;\d+R", sequence) or sequence in {b"\x1b[I", b"\x1b[O"}:
                        self.empty = self.before_escape
                    self.escape.clear()
                continue
            if value == 27:
                if self.pending:
                    output.extend(self.pending)
                    self.pending.clear()
                    self.empty = False
                self.before_escape = self.empty
                self.empty = False
                self.escape.append(value)
                output.append(value)
                continue
            if self.pending:
                if value in (8, 127):
                    self.pending.pop()
                    continue
                if value in (10, 13) and bytes(self.pending) == self.command:
                    flush()
                    actions.append(("monitor", None))
                    self.pending.clear()
                    self.empty = True
                    continue
                candidate = bytes(self.pending) + bytes([value])
                if self.command.startswith(candidate):
                    self.pending.append(value)
                    continue
                output.extend(self.pending)
                self.pending.clear()
                self.empty = False
            elif value == ord("/") and self.empty and not self.paste:
                flush()
                self.pending.append(value)
                continue
            output.append(value)
            if value in (10, 13) and not self.paste:
                self.empty = True
            elif value in (3, 21) and not self.paste:  # Cancel/clear the draft.
                self.empty = True
            elif value >= 32:
                self.empty = False
        flush()
        return actions


class TerminalOutput:
    """Drain Codex while the viewer is open, keeping terminal ownership separate."""
    def __init__(self, fd, inputs):
        self.fd, self.inputs = fd, inputs
        self.lock = threading.RLock()
        self.viewing = False
        self.chunks = deque()
        self.size = 0

    def write(self, data):
        with self.lock:
            if self.viewing:
                self.chunks.append(data)
                self.size += len(data)
                while self.size > 2 * 1024 * 1024:
                    self.size -= len(self.chunks.popleft())
            else:
                os.write(self.fd, data)
                self.command_hint()

    def command_hint(self):
        with self.lock:
            if not self.viewing and self.inputs.pending:
                rows = os.get_terminal_size(self.fd).lines
                text = b"GPU MCP local command: " + bytes(self.inputs.pending)
                os.write(self.fd, b"\x1b7" + f"\x1b[{rows};1H\x1b[2K".encode() + text + b"\x1b8")

    def begin(self):
        with self.lock:
            self.viewing = True

    def end(self):
        with self.lock:
            self.viewing = False
            os.write(self.fd, b"\x1b[?1049h\x1b[2J\x1b[H")
            for chunk in self.chunks:
                os.write(self.fd, chunk)
            self.chunks.clear()
            self.size = 0


NONINTERACTIVE = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control",
                  "completion", "update", "doctor", "debug", "sandbox", "apply", "queue", "archive", "delete",
                  "migrate-rollouts", "unarchive", "cloud", "exec-server", "features", "help"}
VALUE_OPTIONS = {"-c", "--config", "-C", "--cd", "-m", "--model", "-p", "--profile", "-s", "--sandbox",
                 "-a", "--ask-for-approval", "--remote", "--remote-auth-token-env", "--add-dir", "-i", "--image",
                 "--enable", "--disable", "--local-provider", "--code-mode-host"}


def interactive(arguments):
    if any(value in {"--help", "-h", "--version", "-V"} for value in arguments):
        return False
    skip = False
    for value in arguments:
        if skip:
            skip = False
        elif value in VALUE_OPTIONS:
            skip = True
        elif not value.startswith("-"):
            return value not in NONINTERACTIVE
    return True


def options(arguments):
    own, forwarded = {}, []
    iterator = iter(arguments)
    for value in iterator:
        if value == "--":
            forwarded.extend(iterator)
            break
        if value in {"--gpu-mcp-cli", "--gpu-mcp-config", "--gpu-mcp-state-base"}:
            try:
                own[value] = next(iterator)
            except StopIteration:
                raise ValueError(value + " requires a value") from None
        else:
            forwarded.append(value)
    return own, forwarded


def working_directory(arguments):
    directory = Path.cwd()
    for i, value in enumerate(arguments):
        if value in {"-C", "--cd"} and i + 1 < len(arguments):
            directory = Path(arguments[i + 1]).expanduser().absolute()
        elif value.startswith("--cd="):
            directory = Path(value[5:]).expanduser().absolute()
    return directory


def frontend(command, viewer):
    input_fd, output_fd = sys.stdin.fileno(), sys.stdout.fileno()
    saved = termios.tcgetattr(input_fd)
    child, master = pty.fork()
    if child == 0:
        os.execv(command[0], command)
    inputs = MonitorInput()
    output = TerminalOutput(output_fd, inputs)
    done = threading.Event()

    def resize(*args):
        dimensions = fcntl.ioctl(input_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(master, termios.TIOCSWINSZ, dimensions)

    def redraw():
        size = os.get_terminal_size(output_fd)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, size.lines - 1), size.columns, 0, 0))
        resize()

    def drain():
        try:
            while True:
                data = os.read(master, 65536)
                if not data:
                    break
                output.write(data)
        except OSError:
            pass
        finally:
            done.set()

    prior_resize = signal.signal(signal.SIGWINCH, resize)
    try:
        resize()
        tty.setraw(input_fd, termios.TCSANOW)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        while not done.is_set():
            if not select.select([input_fd], [], [], 0.2)[0]:
                continue
            data = os.read(input_fd, 4096)
            if not data:
                break
            for action, value in inputs.feed(data):
                if action == "send":
                    os.write(master, value)
                else:
                    output.begin()
                    try:
                        viewer()
                    except (Exception, SystemExit) as exc:
                        os.write(output_fd, ("\r\nGPU MCP monitor could not open: " + str(exc) +
                            "\r\nPress any key to return to Codex.\r\n").encode())
                        os.read(input_fd, 1)
                    finally:
                        tty.setraw(input_fd, termios.TCSANOW)
                        output.end()
                        redraw()
            output.command_hint()
        _, status = os.waitpid(child, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        signal.signal(signal.SIGWINCH, prior_resize)
        termios.tcsetattr(input_fd, termios.TCSANOW, saved)
        os.close(master)


def main(argv=None):
    own, arguments = options(sys.argv[1:] if argv is None else argv)
    cli = own.get("--gpu-mcp-cli") or os.environ.get("GPU_MCP_CODEX_BIN") or shutil.which("codex")
    if not cli or Path(cli).resolve() == Path(__file__).resolve():
        raise SystemExit("Specify the real Codex executable with --gpu-mcp-cli.")
    command = [str(Path(cli).resolve()), *arguments]
    if not sys.stdin.isatty() or not sys.stdout.isatty() or not interactive(arguments):
        os.execv(command[0], command)
    directory = working_directory(arguments)

    def viewer():
        from gpu_mcp_codex_monitor import main as monitor
        from gpu_mcp_policy_hook import find_policy_file
        policy = own.get("--gpu-mcp-config") or find_policy_file(directory)
        if policy is None:
            raise ValueError("no gpu-mcp.toml found for this Codex session")
        args = ["--config", str(Path(policy).expanduser().absolute())]
        if own.get("--gpu-mcp-state-base"):
            args.extend(["--state-base", own["--gpu-mcp-state-base"]])
        monitor(args)

    raise SystemExit(frontend(command, viewer))


if __name__ == "__main__":
    main()
