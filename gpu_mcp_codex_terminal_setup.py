#!/usr/bin/env python3
"""Install the native monitor's terminal setup once; never replace Codex."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

BEGIN = '# >>> GPU MCP Codex terminal >>>'
END = '# <<< GPU MCP Codex terminal <<<'
TMUX_BEGIN = '# >>> GPU MCP Codex tmux >>>'
TMUX_END = '# <<< GPU MCP Codex tmux <<<'


def shell_name():
    value = os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell
    return Path(value).name


def tmux_available():
    if sys.platform != 'linux' or not shutil.which('tmux'):
        return False
    result = subprocess.run(['tmux', '-V'], capture_output=True, text=True, timeout=5)
    match = re.search(r'tmux (\d+)\.(\d+)', result.stdout)
    return bool(match and tuple(map(int, match.groups())) >= (3, 2))


def tmux_binding(runtime, python, *, state_base=None):
    argv = [str(python), str(Path(runtime) / 'gpu_mcp_codex_tmux.py')]
    if state_base is not None:
        argv += ['--state-base', str(state_base)]
    command = shlex.join(argv) + ' --pane "#{pane_id}" --client #{q:client_name}'
    return 'bind-key -n C-g run-shell -b ' + shlex.quote(command) + '\n'


def tmux_plan(home, runtime, python):
    helper = runtime / 'gpu_mcp_codex_tmux.py'
    if not helper.is_file():
        raise ValueError('update the runtime to include the tmux monitor helper')
    config = home / '.config/gpu-mcp/codex-terminal-tmux.conf'
    planned = {config: tmux_binding(runtime, python)}
    xdg = home / '.config/tmux/tmux.conf'
    roots = [home / '.tmux.conf' if (home / '.tmux.conf').exists() or not xdg.exists() else xdg]
    if (home / '.byobu').is_dir():
        roots.append(home / '.byobu/.tmux.conf')
    block = f'{TMUX_BEGIN}\nsource-file {shlex.quote(str(config))}\n{TMUX_END}'
    for rc in roots:
        if rc.is_symlink() or config.is_symlink():
            raise ValueError('review symlinked tmux configuration before changing it')
        before = rc.read_text() if rc.exists() else ''
        if before.count(TMUX_BEGIN) != before.count(TMUX_END) or before.count(TMUX_BEGIN) > 1:
            raise ValueError('tmux configuration contains an incomplete or duplicate managed block')
        outside = before
        if TMUX_BEGIN in before:
            start, end = before.index(TMUX_BEGIN), before.index(TMUX_END) + len(TMUX_END)
            outside = before[:start] + before[end:]
            after = before[:start] + block + before[end:]
        else:
            after = before + ('\n' if before and not before.endswith('\n') else '') + '\n' + block + '\n'
        if re.search(r'(?m)^\s*bind(?:-key)?\s+[^\n]*\bC-g\b', outside):
            raise ValueError('an existing tmux Ctrl+G binding needs review; it was not replaced')
        planned[rc] = after
    return planned


def live_tmux_binding():
    result = subprocess.run(['tmux', 'display-message', '-p', '#{pid}'], capture_output=True, text=True, timeout=5)
    if result.returncode:
        return None
    key = subprocess.run(['tmux', 'list-keys', '-T', 'root', 'C-g'], capture_output=True, text=True, timeout=5)
    if key.returncode and 'unknown key' not in key.stderr:
        raise ValueError('cannot inspect the current tmux Ctrl+G binding')
    binding = key.stdout.strip()
    if binding and 'gpu_mcp_codex_tmux.py' not in binding:
        raise ValueError('tmux Ctrl+G already has a custom binding; it was not replaced')
    return binding


def plan(home, runtime, python, shell, *, with_tmux=False):
    home, runtime, python = (Path(p).expanduser().resolve() for p in (home, runtime, python))
    if shell not in {'bash', 'zsh', 'fish'}:
        raise ValueError('native terminal setup supports bash, zsh and fish')
    helper = runtime / 'gpu_mcp_codex_editor.py'
    if not helper.is_file() or not python.is_file():
        raise ValueError('the installed helper and Python interpreter must exist')
    visual = shlex.join([str(python), str(helper)])
    state = home / '.config/gpu-mcp'
    if shell == 'fish':
        env_file = state / 'codex-terminal-env.fish'
        rc = home / '.config/fish/config.fish'
        # Fish's quoting follows different rules from POSIX single quotes.
        fish_quote = lambda value: "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
        content = ('if not set -q GPU_MCP_CODEX_EDITOR_ENABLED\n'
                   '    if set -q VISUAL; and test -n "$VISUAL"\n'
                   '        set -gx GPU_MCP_PROMPT_EDITOR "$VISUAL"\n'
                   '    else if set -q EDITOR\n'
                   '        set -gx GPU_MCP_PROMPT_EDITOR "$EDITOR"\n'
                   '    end\nend\n'
                   'set -gx GPU_MCP_CODEX_EDITOR_ENABLED 1\n'
                   f'set -gx VISUAL {fish_quote(visual)}\n')
        block = f'{BEGIN}\nif test -r {fish_quote(str(env_file))}\n    source {fish_quote(str(env_file))}\nend\n{END}'
    else:
        env_file = state / 'codex-terminal-env.sh'
        rc = home / ('.bashrc' if shell == 'bash' else '.zshrc')
        content = ('if [ "${GPU_MCP_CODEX_EDITOR_ENABLED-}" != 1 ]; then\n'
                   '    export GPU_MCP_PROMPT_EDITOR="${VISUAL:-${EDITOR:-}}"\n'
                   'fi\nexport GPU_MCP_CODEX_EDITOR_ENABLED=1\n'
                   f'export VISUAL={shlex.quote(visual)}\n')
        block = f'{BEGIN}\nif [ -r {shlex.quote(str(env_file))} ]; then\n    . {shlex.quote(str(env_file))}\nfi\n{END}'
    if rc.is_symlink() or env_file.is_symlink():
        raise ValueError('review symlinked terminal configuration before changing it')
    before = rc.read_text() if rc.exists() else ''
    if before.count(BEGIN) != before.count(END) or before.count(BEGIN) > 1:
        raise ValueError('terminal configuration contains an incomplete or duplicate managed block')
    if BEGIN in before:
        start, end = before.index(BEGIN), before.index(END) + len(END)
        after = before[:start] + block + before[end:]
    else:
        after = before + ('\n' if before and not before.endswith('\n') else '') + '\n' + block + '\n'
    planned = {env_file: content, rc: after}
    if with_tmux:
        planned.update(tmux_plan(home, runtime, python))
    return planned


def install(home, runtime, python, shell, *, preview=False, with_tmux=False, activate_tmux=False):
    previous_binding = live_tmux_binding() if with_tmux and activate_tmux else None
    changes = {path: text for path, text in plan(home, runtime, python, shell, with_tmux=with_tmux).items()
               if not path.exists() or path.read_text() != text}
    if preview:
        return {'changed_files': [str(path) for path in changes], 'preview': True,
                'tmux_live_binding': previous_binding, 'tmux_popup':with_tmux}
    result = {'changed_files':[], 'preview':False}
    if changes:
        result = write_changes(changes)
    if with_tmux and activate_tmux and previous_binding is not None:
        config = Path(home).expanduser().resolve() / '.config/gpu-mcp/codex-terminal-tmux.conf'
        applied = subprocess.run(['tmux', 'source-file', str(config)], capture_output=True, text=True, timeout=5)
        if applied.returncode:
            raise ValueError('tmux shortcut activation failed: ' + applied.stderr.strip())
        current = live_tmux_binding()
        if not current or 'gpu_mcp_codex_tmux.py' not in current:
            raise ValueError('tmux did not retain the monitor shortcut')
        result['tmux_activation'] = 'active for existing panes; press Ctrl+G in Codex'
    elif with_tmux:
        result['tmux_activation'] = 'configured for future tmux/Byobu sessions'
    result['activation'] = ('Ctrl+G is active in existing tmux panes. Outside tmux, new shells load the editor helper.'
        if result.get('tmux_activation', '').startswith('active') else
        'New shells load this automatically. Existing non-tmux shells must load the saved setting once.')
    return result


def write_changes(changes):
    backup = Path(tempfile.mkdtemp(prefix='gpu_mcp_terminal_setup_'))
    recorded = {}
    for index, (path, text) in enumerate(changes.items()):
        existed = path.exists()
        if existed:
            shutil.copy2(path, backup / f'{index}-{path.name}')
        recorded[str(path)] = {'existed': existed, 'backup': str(backup / f'{index}-{path.name}') if existed else None}
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if existed else 0o600
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(text)
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    (backup / 'ledger.json').write_text(json.dumps({
        'created_at': datetime.now(timezone.utc).isoformat(), 'changes': recorded,
    }, indent=2) + '\n')
    return {'changed_files': list(recorded), 'backup_directory': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--shell', choices=['bash', 'zsh', 'fish'], default=shell_name())
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--no-tmux', action='store_true', help='configure only the native external-editor entry')
    args = parser.parse_args(argv)
    try:
        result = install(args.home, args.runtime, args.python, args.shell, preview=args.preview,
                         with_tmux=not args.no_tmux and tmux_available(), activate_tmux=True)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
