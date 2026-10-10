#!/usr/bin/env python3
"""Install the native monitor's terminal setup once; never replace Codex."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import sys
import tempfile

BEGIN = '# >>> GPU MCP Codex terminal >>>'
END = '# <<< GPU MCP Codex terminal <<<'


def shell_name():
    value = os.environ.get('SHELL') or pwd.getpwuid(os.getuid()).pw_shell
    return Path(value).name


def plan(home, runtime, python, shell):
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
    return {env_file: content, rc: after}


def install(home, runtime, python, shell, *, preview=False):
    changes = {path: text for path, text in plan(home, runtime, python, shell).items()
               if not path.exists() or path.read_text() != text}
    if preview or not changes:
        return {'changed_files': [str(path) for path in changes], 'preview': preview}
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
    return {'changed_files': list(recorded), 'backup_directory': str(backup),
            'activation': 'New terminal sessions load this automatically. Keep using codex or codex resume.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--shell', choices=['bash', 'zsh', 'fish'], default=shell_name())
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = install(args.home, args.runtime, args.python, args.shell, preview=args.preview)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
