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
import socket
import subprocess
import sys
import tomllib


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


def repo_choices(config=None, base=None, *, home=None):
    """List known registrations and histories in a stable order, without startup."""
    from gpu_mcp_policy_hook import find_policy_file
    from gpu_mcp_codex_launcher import read_info
    explicit_home = home is not None
    home = Path(home) if explicit_home else Path.home()
    choices, candidates = set(), set()

    def add(policy):
        try:
            if policy.is_file():
                choices.add(policy.resolve())
        except (OSError, ValueError):
            pass

    def read(path, parser):
        try:
            result = parser(path.read_text())
            return result if isinstance(result, dict) else {}
        except (OSError, ValueError):
            return {}

    current = Path(config).resolve() if config else find_policy_file(Path.cwd())
    if current:
        add(Path(current))
    approvals = read(home / 'gpu-mcp/state/approved-policies.json', json.loads)
    candidates.update(Path(p).parent for p in approvals if Path(p).is_absolute())
    codex_homes = {home / '.codex'}
    if not explicit_home and os.environ.get('CODEX_HOME'):
        codex_homes.add(Path(os.environ['CODEX_HOME']).expanduser())
    for codex_home in codex_homes:
        projects = read(codex_home / 'config.toml', tomllib.loads).get('projects', {})
        if isinstance(projects, dict):
            candidates.update(Path(p) for p in projects if Path(p).is_absolute())
    for repo in candidates:
        servers = read(repo / '.codex/config.toml', tomllib.loads).get('mcp_servers', {})
        server = servers.get('gpu-cluster-mcp', {}) if isinstance(servers, dict) else {}
        if not isinstance(server, dict) or server.get('enabled', True) is False:
            continue
        args = server.get('args', [])
        if not isinstance(args, list) or not all(isinstance(p, str) for p in args):
            continue
        if not any(Path(p).name in {'gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'} for p in args):
            continue
        if '--config' not in args or args.index('--config') + 1 >= len(args):
            continue
        policy = Path(args[args.index('--config') + 1]).expanduser()
        try:
            if policy.is_absolute() and policy.parent.resolve() == repo.resolve():
                add(policy)
        except (OSError, ValueError):
            continue
    state = Path(base) if base else home / 'gpu-mcp/state/codex'
    if state.is_dir():
        for root in sorted(state.iterdir()):
            info = read_info(root)
            if (isinstance(info, dict) and info.get('hostname') == socket.gethostname()
                    and isinstance(info.get('config_path'), str)):
                add(Path(info['config_path']))
    return sorted(choices, key=lambda p: (p.parent.name.casefold(), str(p)))


def repo_rows(policies):
    """Place each descendant beneath its nearest listed repository ancestor."""
    repos = {policy.parent for policy in policies}
    children = {}
    for policy in policies:
        parent = next((p for p in policy.parent.parents if p in repos), None)
        children.setdefault(parent, []).append(policy)

    def rows(parent=None, depth=0):
        def label(policy):
            return str(policy.parent.relative_to(parent)) if parent else policy.parent.name

        for policy in sorted(children.get(parent, []), key=lambda p: (label(p).casefold(), str(p))):
            text = '  ' * depth + label(policy)
            if parent is None:
                text += f' — {policy.parent}'
            yield text, policy
            yield from rows(policy.parent, depth + 1)

    return list(rows())


def choose(screen, policies, base, *, allow_edit=True):
    from gpu_mcp_codex_monitor import connect_viewer, view
    from gpu_mcp_monitor_display import safe_text
    from gpu_mcp_policy_hook import find_policy_file
    curses.curs_set(0)
    curses.set_escdelay(25)
    screen.keypad(True)
    # Put the distinguishing name first so a narrow split does not show only
    # the same long filesystem prefix for every repository.
    rows = repo_rows(policies)
    if allow_edit:
        rows.append(('Edit prompt in your text editor', 'edit'))
    rows.append(('Cancel', None))
    current = find_policy_file(Path.cwd())
    selected = next((i for i, (_, policy) in enumerate(rows)
                     if current and policy == current.resolve()), 0)
    error = ''
    while True:
        screen.timeout(-1)
        screen.erase()
        height, width = screen.getmaxyx()
        lines = ['GPU MCP — human terminal', '↑↓ Select | Enter/→ Open | Esc Return to Codex', '']
        if not policies:
            lines.append('No GPU MCP repository found.')
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
