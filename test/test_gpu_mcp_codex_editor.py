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


def test_repo_chooser_keeps_stopped_backends(tmp_path, monkeypatch):
    import json
    import socket
    import gpu_mcp_codex_editor as editor
    current, stopped = tmp_path / 'current', tmp_path / 'stopped'
    base = tmp_path / 'state'
    for directory in (current, stopped, base / 'old'):
        directory.mkdir(parents=True)
    for repo in (current, stopped):
        (repo / 'gpu-mcp.toml').write_text('schema_version=1\n')
    (base / 'old/backend.json').write_text(json.dumps({
        'hostname': socket.gethostname(), 'pid': 99999999,
        'config_path': str(stopped / 'gpu-mcp.toml')}))
    monkeypatch.chdir(current)
    assert editor.repo_choices(base=base, home=tmp_path) == [current / 'gpu-mcp.toml', stopped / 'gpu-mcp.toml']


@pytest.mark.parametrize('entry', ['gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'])
def test_idle_monitor_never_starts_a_registered_server(tmp_path, monkeypatch, entry):
    import json
    import gpu_mcp_codex_monitor as monitor
    repo = tmp_path / 'repo'
    (repo / '.codex').mkdir(parents=True)
    config = repo / 'gpu-mcp.toml'
    config.write_text('')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    launcher = runtime / entry
    launcher.write_text('')
    python = tmp_path / 'server-python'
    python.write_text('')
    (repo / '.codex/config.toml').write_text(
        '[mcp_servers.gpu-cluster-mcp]\ncommand=' + json.dumps(str(python)) + '\nargs=' +
        json.dumps([str(launcher), '--config', str(config)]) + '\n'
        '[mcp_servers.gpu-cluster-mcp.env]\nGPU_MCP_PYTHON="/registered/job/python"\n')
    monkeypatch.setattr(monitor, 'read_info', lambda *a: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('viewing must not start a server'))
    viewer = monitor.connect_viewer(config, base=tmp_path / 'states')
    snapshot = monitor.request(viewer, '/human/snapshot', human=True)
    assert snapshot['saved_only'] is True
    assert snapshot['repo_root'] == str(repo)
    assert snapshot['jobs'] == []
    assert not (tmp_path / 'states').exists()


def test_catalog_includes_legacy_registrations_independent_of_cwd_and_backend_liveness(tmp_path, monkeypatch):
    import json
    import socket

    home, base = tmp_path / 'home', tmp_path / 'backends'
    (home / 'gpu-mcp/state').mkdir(parents=True)
    (home / '.codex').mkdir()
    policies = []
    for name, entry in [('Polycomb_Structure', 'gpu_mcp_server.py'),
                        ('custom-openmm', 'gpu_mcp_server.py'),
                        ('shared', 'gpu_mcp_codex_launcher.py')]:
        repo = tmp_path / name
        (repo / '.codex').mkdir(parents=True)
        policy = repo / 'gpu-mcp.toml'
        policy.write_text('schema_version=1\n')
        (repo / '.codex/config.toml').write_text(
            '[mcp_servers.gpu-cluster-mcp]\nenabled=true\nargs=' +
            json.dumps(['/installed/' + entry, '--config', str(policy)]) + '\n')
        policies.append(policy)
    # Approval and trusted-project history are independent discovery sources.
    (home / 'gpu-mcp/state/approved-policies.json').write_text(json.dumps({str(policies[0]): {}}))
    (home / '.codex/config.toml').write_text('[projects.' + json.dumps(str(policies[1].parent)) + ']\n')
    (base / 'shared').mkdir(parents=True)
    info = base / 'shared/backend.json'
    info.write_text(json.dumps({'hostname': socket.gethostname(), 'pid': 99999999,
                                'config_path': str(policies[2])}))
    broken = tmp_path / 'broken'
    (broken / '.codex').mkdir(parents=True)
    (broken / 'gpu-mcp.toml').write_text('schema_version=1\n')
    (broken / '.codex/config.toml').write_text('invalid [ toml')
    with (home / '.codex/config.toml').open('a') as stream:
        stream.write('[projects.' + json.dumps(str(broken)) + ']\n')
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    expected = sorted(policies, key=lambda p: (p.parent.name.casefold(), str(p)))
    for current in policies:
        monkeypatch.chdir(current.parent)
        assert editor.repo_choices(base=base) == expected
    # Restarting changes backend PID, not membership or ordering.
    record = json.loads(info.read_text())
    record['pid'] = os.getpid()
    info.write_text(json.dumps(record))
    assert editor.repo_choices(base=base) == expected


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


def test_hierarchy_groups_descendants_and_retains_current_child_selection(monkeypatch):
    import gpu_mcp_codex_monitor as monitor
    import gpu_mcp_policy_hook as hook
    from test_gpu_mcp_codex_monitor import Screen

    monkeypatch.setattr(editor.curses, 'curs_set', lambda value: None)
    monkeypatch.setattr(editor.curses, 'set_escdelay', lambda value: None)
    parent = Path('/fixture/parent/gpu-mcp.toml')
    child = parent.parent / 'experiments/alpha/gpu-mcp.toml'
    grandchild = child.parent / 'nested/gpu-mcp.toml'
    sibling = Path('/fixture/parent-other/gpu-mcp.toml')
    # The input order differs from the tree order. A textual prefix is not a parent.
    policies = [grandchild, child, sibling, parent]
    monkeypatch.setattr(hook, 'find_policy_file', lambda cwd: child)
    opened = []
    returns = iter(['chooser', None])
    monkeypatch.setattr(monitor, 'connect_viewer', lambda policy, **kw: policy)

    def view(screen, info, **kw):
        opened.append(info)
        return next(returns)

    monkeypatch.setattr(monitor, 'view', view)
    screen = Screen([10, 10])
    editor.choose(screen, policies, None)
    lines = screen.frames[0].splitlines()
    assert lines.index('parent — /fixture/parent') < lines.index('  experiments/alpha')
    assert lines.index('  experiments/alpha') < lines.index('    nested')
    assert 'parent-other — /fixture/parent-other' in lines
    assert opened == [child, child], 'tree ordering or returning to the chooser changed the selection'
