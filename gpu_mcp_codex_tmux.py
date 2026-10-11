#!/usr/bin/env python3
"""Human Ctrl+G popup for existing tmux panes, independent of editor variables."""
from __future__ import annotations

import argparse
import curses
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


def tmux(*args, check=True, timeout=5):
    result = subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or 'tmux command failed')
    return result


def process(pid, proc_root=Path('/proc')):
    root = proc_root / str(pid)
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid':pid, 'pgid':int(fields[2]), 'foreground':int(fields[5]),
            'start':fields[19], 'exe':Path(os.readlink(root / 'exe')).name,
            'argv':(root / 'cmdline').read_bytes().split(b'\0'),
            'children':[int(v) for v in (root / 'task' / str(pid) / 'children').read_text().split()]}


def frontend(pane_pid, proc_root=Path('/proc')):
    """Handle both native Codex and npm's node -> codex process group."""
    try:
        group = process(pane_pid, proc_root)['foreground']
        if group <= 0:
            return None
        pending, seen = [group], set()
        while pending and len(seen) < 64:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            item = process(pid, proc_root)
            if item['pgid'] != group:
                continue
            if item['exe'] == 'codex' and not any(v in item['argv'][1:] for v in
                    (b'app-server', b'exec-server', b'exec', b'review')):
                return {k:item[k] for k in ('pid','pgid','start')}
            pending.extend(item['children'])
    except (OSError, ValueError, IndexError):
        pass
    return None


def pane_info(pane):
    if not re.fullmatch(r'%\d+', pane):
        raise ValueError('an exact tmux pane ID is required')
    raw = tmux('display-message', '-p', '-t', pane, '#{pane_pid}\n#{pane_current_path}').stdout.rstrip('\n')
    pid, directory = raw.split('\n', 1)
    return int(pid), directory


def dispatch(pane, client=None, *, probe=False, state_base=None):
    pane_pid, directory = pane_info(pane)
    matched = frontend(pane_pid)
    result = {'pane':pane, 'directory':directory, 'frontend':matched,
              'action':'popup' if matched else 'passthrough'}
    if probe:
        return result
    if not matched or frontend(pane_pid) != matched:
        tmux('send-keys', '-t', pane, 'C-g')
        return result
    command = [sys.executable, str(Path(__file__).resolve()), '--popup', '--pane', pane,
               '--frontend', str(matched['pid']), '--start', matched['start']]
    if state_base:
        command += ['--state-base', str(state_base)]
    args = ['display-popup', '-E', '-w', '95%', '-h', '95%', '-t', pane, '-d', directory]
    if client:
        args += ['-c', client]
    # tmux 3.2 accepts one shell-command. exec keeps tmux as the viewer's parent.
    tmux(*args, 'exec ' + shlex.join(command), timeout=None)
    return result


def popup(pane, pid, start, state_base=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('open the monitor with Ctrl+G in a human tmux client')
    if Path(os.readlink(Path('/proc') / str(os.getppid()) / 'exe')).name != 'tmux':
        raise ValueError('the human popup must be opened directly by tmux')
    pane_pid, _ = pane_info(pane)
    matched = frontend(pane_pid)
    if not matched or matched['pid'] != pid or matched['start'] != start:
        raise ValueError('the Codex frontend changed before the popup opened')
    from gpu_mcp_codex_editor import choose, repo_choices
    curses.wrapper(lambda screen: choose(screen, repo_choices(base=state_base), state_base, allow_edit=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pane', required=True)
    parser.add_argument('--client')
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--popup', action='store_true')
    parser.add_argument('--frontend', type=int)
    parser.add_argument('--start')
    parser.add_argument('--state-base', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.popup:
            popup(args.pane, args.frontend, args.start, args.state_base)
        else:
            result = dispatch(args.pane, args.client, probe=args.probe, state_base=args.state_base)
            if args.probe:
                print(json.dumps(result))  # Process metadata only; never job/viewer data.
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if args.popup or args.probe:
            print(str(exc), file=sys.stderr)
        else:
            tmux('display-message', '-t', args.pane, 'GPU MCP monitor: ' + str(exc)[:200], check=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
