#!/usr/bin/env python3
"""Human monitor through Codex's native external-editor keyboard action."""
from __future__ import annotations

import argparse
import curses
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def native_draft(path: Path) -> bool:
    """Recognize protected Codex editor files, rather than unrelated editors."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            return False
        resolved = path.resolve()
        homes = {Path.home() / '.codex'}
        if os.environ.get('CODEX_HOME'):
            homes.add(Path(os.environ['CODEX_HOME']).expanduser())
        homes.update(parent / '.codex' for parent in Path.cwd().resolve().parents)
        homes.add(Path.cwd().resolve() / '.codex')
        return any(resolved.parent == (home / 'editor').resolve() for home in homes)
    except OSError:
        return False


def frontend_parent() -> bool:
    """Avoid exposing a human view through an ordinary agent shell/PTY call."""
    try:
        proc = Path('/proc') / str(os.getppid())
        if proc.exists():
            executable = Path(os.readlink(proc / 'exe')).name
            arguments = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
        else:
            executable = Path(subprocess.check_output(
                ['ps', '-p', str(os.getppid()), '-o', 'comm='], text=True).strip()).name
            arguments = shlex.split(subprocess.check_output(
                ['ps', '-p', str(os.getppid()), '-o', 'args='], text=True))
        return executable == 'codex' and not any(
            value in {'app-server', 'exec-server', 'exec', 'review'} for value in arguments[1:])
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def editor_command() -> list[str]:
    original = os.environ.get('GPU_MCP_PROMPT_EDITOR', '') or os.environ.get('EDITOR', '')
    if original:
        command = shlex.split(original)
        # Refuse recursive delegation caused by a misconfigured old setup.
        if any(Path(value).name == Path(__file__).name for value in command):
            raise ValueError('the saved prompt editor points back to the monitor helper')
        if command:
            return command
    fallback = shutil.which('editor') or shutil.which('vi')
    if not fallback:
        raise ValueError('no text editor is installed')
    return [fallback]


def repo_choices(config=None, base=None):
    """Prefer the current policy; offer live repos when frontend cwd differs."""
    from gpu_mcp_policy_hook import find_policy_file
    from gpu_mcp_codex_launcher import alive, read_info
    choices = []
    current = Path(config).resolve() if config else find_policy_file(Path.cwd())
    if current:
        choices.append(Path(current).resolve())
    state = Path(base) if base else Path.home() / 'gpu-mcp/state/codex'
    if state.is_dir():
        for root in sorted(state.iterdir()):
            info = read_info(root)
            if alive(info) and info.get('config_path'):
                policy = Path(info['config_path'])
                if policy.is_file() and policy.resolve() not in choices:
                    choices.append(policy.resolve())
    return choices


def choose(screen, policies, base, *, allow_edit=True):
    from gpu_mcp_codex_monitor import connect_viewer, view
    from gpu_mcp_monitor_display import safe_text
    curses.curs_set(0)
    curses.set_escdelay(25)
    screen.keypad(True)
    rows = [(f'Monitor jobs — {policy.parent}', policy) for policy in policies]
    if allow_edit:
        rows.append(('Edit prompt in your text editor', 'edit'))
    rows.append(('Cancel', None))
    selected, error = 0, ''
    while True:
        screen.timeout(-1)
        screen.erase()
        height, width = screen.getmaxyx()
        lines = ['GPU MCP — human terminal', '↑↓ Select | Enter/→ Open | Esc Return to Codex', '']
        if not policies:
            lines.append('No GPU MCP policy or live repository found.')
        start = max(0, selected - max(1, height - len(lines) - 4) + 1)
        for y, line in enumerate(lines):
            if y < height - 1:
                screen.addnstr(y, 0, line, max(1, width - 1))
        for n, (label, _) in enumerate(rows[start:start + max(1, height - len(lines) - 3)]):
            style = curses.A_REVERSE if n + start == selected else 0
            screen.addnstr(len(lines) + n, 0, safe_text(label), max(1, width - 1), style)
        if error and height > 2:
            screen.addnstr(height - 2, 0, safe_text(error), max(1, width - 1))
        screen.refresh()
        key = screen.getch()
        if key in (27, 3, ord('q')):
            return None
        if key in (curses.KEY_DOWN, ord('j')):
            selected = min(len(rows) - 1, selected + 1)
        if key in (curses.KEY_UP, ord('k')):
            selected = max(0, selected - 1)
        if key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            value = rows[selected][1]
            if value is None or value == 'edit':
                return value
            try:
                info = connect_viewer(value, base=base)
                if view(screen, info, chooser=True) != 'chooser':
                    return None
                error = ''
            except Exception as exc:
                error = str(exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state-base', type=Path)
    parser.add_argument('draft', type=Path)
    args = parser.parse_args(argv)
    try:
        if not native_draft(args.draft):
            return subprocess.call([*editor_command(), str(args.draft)])
        if not sys.stdin.isatty() or not sys.stdout.isatty() or not frontend_parent():
            raise ValueError('open this view through Codex\'s keyboard action in a human terminal')
        # Never write monitor information to the file Codex reads back as a draft.
        choice = curses.wrapper(choose, repo_choices(args.config, args.state_base), args.state_base)
        if choice == 'edit':
            return subprocess.call([*editor_command(), str(args.draft)])
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
