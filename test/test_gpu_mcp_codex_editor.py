"""Native keyboard setup, editor preservation and human-view boundaries."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

import gpu_mcp_codex_editor as editor
from gpu_mcp_codex_terminal_setup import BEGIN, install, plan

ROOT = Path(__file__).resolve().parents[1]


def test_install_preserves_shell_settings_and_is_idempotent(tmp_path):
    rc = tmp_path / '.bashrc'
    original = "# User settings\nexport EDITOR='vim -n'\n"
    rc.write_text(original)
    result = install(tmp_path, ROOT, sys.executable, 'bash')
    assert rc.read_text().startswith(original)
    assert rc.read_text().count(BEGIN) == 1
    assert (Path(result['backup_directory']) / '1-.bashrc').read_text() == original
    assert install(tmp_path, ROOT, sys.executable, 'bash')['changed_files'] == []


def test_environment_preserves_original_editor_across_repeated_loading(tmp_path):
    install(tmp_path, ROOT, sys.executable, 'bash')
    env_file = tmp_path / '.config/gpu-mcp/codex-terminal-env.sh'
    command = 'VISUAL="code --wait"; . ' + shlex.quote(str(env_file))
    command += '; . ' + shlex.quote(str(env_file))
    command += '; printf "%s\\n%s\\n" "$GPU_MCP_PROMPT_EDITOR" "$VISUAL"'
    env = {k: v for k, v in os.environ.items() if not k.startswith('GPU_MCP_')}
    saved, visual = subprocess.check_output(['bash', '--noprofile', '--norc', '-c', command],
                                          text=True, env=env).splitlines()
    assert saved == 'code --wait'
    assert shlex.split(visual) == [str(Path(sys.executable).resolve()), str(ROOT / 'gpu_mcp_codex_editor.py')]


def test_preview_and_incomplete_block_do_not_write(tmp_path):
    assert install(tmp_path, ROOT, sys.executable, 'zsh', preview=True)['changed_files']
    assert not list(tmp_path.iterdir())
    (tmp_path / '.bashrc').write_text(BEGIN + '\n')
    with pytest.raises(ValueError, match='incomplete'):
        install(tmp_path, ROOT, sys.executable, 'bash')
    assert not (tmp_path / '.config').exists()


def test_native_draft_monitor_keeps_prompt_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path / 'editor/draft.md'
    path.parent.mkdir()
    original = b'Existing human draft\nwith two lines.\n'
    path.write_bytes(original)
    monkeypatch.setattr(editor, 'frontend_parent', lambda: True)
    monkeypatch.setattr(editor.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(editor.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(editor, 'repo_choices', lambda *args: [])
    monkeypatch.setattr(editor.curses, 'wrapper', lambda *args: None)
    assert editor.main([str(path)]) == 0
    assert path.read_bytes() == original


def test_agent_style_caller_cannot_open_private_monitor(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path / 'editor/draft.md'
    path.parent.mkdir()
    path.write_text('draft')
    monkeypatch.setattr(editor, 'frontend_parent', lambda: False)
    monkeypatch.setattr(editor.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(editor.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(editor, 'repo_choices', lambda *args: pytest.fail('private view was read'))
    assert editor.main([str(path)]) == 1
    assert 'keyboard action' in capsys.readouterr().err


def test_other_applications_keep_their_editor(tmp_path, monkeypatch):
    path = tmp_path / 'ordinary draft.md'
    path.write_text('ordinary')
    monkeypatch.setenv('GPU_MCP_PROMPT_EDITOR', 'code --wait')
    called = []
    monkeypatch.setattr(editor.subprocess, 'call', lambda argv: called.append(argv) or 0)
    assert editor.main([str(path)]) == 0
    assert called == [['code', '--wait', str(path)]]


def test_fish_and_zsh_profiles_have_managed_setup(tmp_path):
    for shell in ('zsh', 'fish'):
        planned = plan(tmp_path / shell, ROOT, sys.executable, shell)
        assert len(planned) == 2
        rc = next(path for path in planned if path.name in {'.zshrc', 'config.fish'})
        env = next(path for path in planned if path != rc)
        assert BEGIN in planned[rc] and str(env) in planned[rc]
        assert 'GPU_MCP_PROMPT_EDITOR' in planned[env]


def test_return_to_chooser_keeps_repository_selection(monkeypatch):
    import gpu_mcp_codex_monitor as monitor
    from test_gpu_mcp_codex_monitor import Screen

    monkeypatch.setattr(editor.curses, 'curs_set', lambda value: None)
    monkeypatch.setattr(editor.curses, 'set_escdelay', lambda value: None)
    policies = [Path('/fixture/first/gpu-mcp.toml'), Path('/fixture/second/gpu-mcp.toml')]
    opened = []
    returns = iter(['chooser', None])
    monkeypatch.setattr(monitor, 'connect_viewer', lambda policy, **kwargs: policy)

    def view(screen, info, **kwargs):
        assert kwargs == {'chooser': True}
        opened.append(info)
        return next(returns)

    monkeypatch.setattr(monitor, 'view', view)
    screen = Screen([editor.curses.KEY_DOWN, editor.curses.KEY_RIGHT, 10])
    assert editor.choose(screen, policies, None) is None
    assert opened == [policies[1], policies[1]]
