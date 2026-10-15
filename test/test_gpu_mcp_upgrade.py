"""The user-facing upgrade command preserves live work and local choices."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

import gpu_mcp_upgrade as upgrade
from test_gpu_mcp_codex_upgrade import installed, job


@pytest.fixture
def setup(installed, monkeypatch):
    f = installed
    f.home = f.repo.parent / 'home'
    f.home.mkdir()
    f.codex_home = f.home / '.codex'
    f.codex_home.mkdir()
    f.registration.write_text(f.registration.read_text().replace('/original/python', sys.executable))
    (f.runtime / 'gpu_mcp_codex_editor.py').write_text('# editor helper\n')
    monkeypatch.setattr(upgrade, 'probe_python', lambda *args: {'missing': [], 'signature': 'current'})
    monkeypatch.setattr(upgrade.launcher, 'code_hash', lambda: 'current-code')
    monkeypatch.setattr(upgrade, 'repair_ssh', lambda *a, **k: {'repaired': [], 'refreshed': [], 'deferred': []})
    monkeypatch.setattr(upgrade, 'running_servers', lambda: [])
    monkeypatch.chdir(f.repo)
    f.run = lambda **kw: upgrade.upgrade(f.runtime, repos=[f.repo], home=f.home,
                                       codex_home=f.codex_home, store=f.store,
                                       shell='bash', with_tmux=False, **kw)
    return f


def test_apply_and_repeat_preserve_policy_choices_and_other_servers(setup):
    f = setup
    f.registration.write_text(f.registration.read_text() + '''
[mcp_servers.gpu-cluster-mcp.tools.manage_gpu_job]
approval_mode = "prompt"
[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]
approval_mode = "deny"
''')
    original = tomllib.loads(f.registration.read_text())
    policy, approvals = f.policy.read_bytes(), f.store.read_bytes()
    first = f.run()
    result = tomllib.loads(f.registration.read_text())
    before, after = (d['mcp_servers']['gpu-cluster-mcp'] for d in (original, result))
    assert 'gpu_mcp_codex_launcher.py' in after['args'][0]
    assert after['env'] == before['env'] and after['command'] == before['command']
    assert after['tools']['manage_gpu_job']['approval_mode'] == 'prompt'
    assert after['tools']['kill_gpu_process']['approval_mode'] == 'deny'
    assert after['tools']['build_python_artifact']['approval_mode'] == 'approve'
    assert after['tools']['reload_policy']['approval_mode'] == 'prompt'
    assert result['mcp_servers']['other'] == original['mcp_servers']['other']
    assert f.policy.read_bytes() == policy and f.store.read_bytes() == approvals
    assert Path(first['backup_directory']).is_dir()
    assert '.config/gpu-mcp/codex-terminal-env.sh' in (f.home / '.bashrc').read_text()
    second = f.run()
    assert second['changed_files'] == [] and second['terminal']['changed_files'] == []


def test_preview_does_not_write_stop_or_install(setup, monkeypatch):
    before = setup.registration.read_bytes()
    monkeypatch.setattr(upgrade, 'stop_idle', lambda *a: pytest.fail('preview stopped a backend'))
    monkeypatch.setattr(upgrade.subprocess, 'run', lambda *a, **kw: pytest.fail('preview installed dependencies'))
    setup.run(preview=True)
    assert setup.registration.read_bytes() == before
    assert not (setup.home / '.bashrc').exists()
    assert not (setup.home / 'gpu-mcp').exists()


def test_discovery_uses_known_paths_and_ignores_other_installs(setup):
    f = setup
    unrelated = f.repo.parent / 'unrelated'
    (unrelated / '.codex').mkdir(parents=True)
    (unrelated / '.codex/config.toml').write_text('[mcp_servers.gpu-cluster-mcp]\nargs=["/other/gpu_mcp_server.py"]\n')
    (f.codex_home / 'config.toml').write_text(f'[projects.{json.dumps(str(unrelated))}]\ntrust_level="trusted"\n')
    assert upgrade.discovery(f.runtime, f.home, f.codex_home, f.store) == [f.repo]
    assert set(upgrade.discovery(f.runtime, f.home, f.codex_home, f.store, all_installations=True)) == {f.repo, unrelated}


def test_copied_runtime_registration_is_not_an_extra_repository(setup):
    f = setup
    (f.runtime / '.codex').mkdir()
    (f.runtime / '.codex/config.toml').write_text(f.registration.read_text())
    (f.codex_home / 'config.toml').write_text(f'[projects.{json.dumps(str(f.runtime))}]\ntrust_level="trusted"\n')
    assert upgrade.discovery(f.runtime, f.home, f.codex_home, f.store) == [f.repo]


def test_active_legacy_job_blocks_all_configuration_changes(setup):
    job(setup)
    before = setup.registration.read_bytes()
    result = setup.run()
    assert result['deferred_upgrade'] and result['changed_files'] == []
    assert 'finish its legacy jobs' in result['deferred_upgrade'][0]
    assert setup.registration.read_bytes() == before
    assert not (setup.home / '.bashrc').exists()


def shared_backend(f, monkeypatch, *, active=False, code='older-code'):
    f.registration.write_text(f.registration.read_text().replace('gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'))
    info = {'server_instance_id': 'own', 'code_hash': code, 'runtime_signature': 'current',
            'config_path': str(f.policy), 'live': True}
    monkeypatch.setattr(upgrade.launcher, 'read_info', lambda root: info)
    monkeypatch.setattr(upgrade.launcher, 'alive', lambda value: bool(value and value.get('live')))
    requests = []

    def request(value, path, **kwargs):
        requests.append(path)
        if path == '/human/snapshot':
            return {'server_instance_id': 'own', 'jobs':
                    [{'server_instance_id': 'own', 'job_lifecycle': 'running', 'active_reservation': True}] if active else []}
        assert path == '/human/stop-if-idle'
        value['live'] = False
        return {'status': 'stopping'}

    monkeypatch.setattr(upgrade.launcher, 'request', request)
    return info, requests


def test_old_busy_backend_blocks_without_stopping_it(setup, monkeypatch):
    info, requests = shared_backend(setup, monkeypatch, active=True)
    before = setup.registration.read_bytes()
    with pytest.raises(ValueError, match='finish 1 active job'):
        setup.run()
    assert info['live'] and '/human/stop-if-idle' not in requests
    assert setup.registration.read_bytes() == before


def test_current_backend_with_active_jobs_is_left_running(setup, monkeypatch):
    info, requests = shared_backend(setup, monkeypatch, active=True, code='current-code')
    setup.run()
    assert info['live'] and requests == []


def test_idle_old_backend_is_refreshed_once(setup, monkeypatch):
    info, requests = shared_backend(setup, monkeypatch)
    setup.run()
    assert not info['live']
    assert requests.count('/human/stop-if-idle') == 1


def test_busy_race_at_shutdown_refuses_without_overwriting_config(setup, monkeypatch):
    info, requests = shared_backend(setup, monkeypatch)
    original_request = upgrade.launcher.request

    def race(value, path, **kwargs):
        if path == '/human/stop-if-idle':
            return {'status': 'refused', 'reason': 'active request arrived'}
        return original_request(value, path, **kwargs)

    monkeypatch.setattr(upgrade.launcher, 'request', race)
    before = setup.registration.read_bytes()
    with pytest.raises(ValueError, match='became busy'):
        setup.run()
    assert setup.registration.read_bytes() == before and info['live']


def test_registration_edit_during_dependency_install_is_not_lost(setup, monkeypatch):
    probes = [0]

    def probe(*args):
        probes[0] += 1
        return {'missing': ['mcp'] if probes[0] == 1 else [], 'signature': 'current'}

    def install(argv, **kwargs):
        assert argv[1:4] == ['-m', 'pip', 'install']
        setup.registration.write_text(setup.registration.read_text() + '\n# concurrent user edit\n')

    monkeypatch.setattr(upgrade, 'probe_python', probe)
    monkeypatch.setattr(upgrade.subprocess, 'run', install)
    with pytest.raises(ValueError, match='changed during upgrade'):
        setup.run()
    assert setup.registration.read_text().endswith('# concurrent user edit\n')
    assert not (setup.home / '.bashrc').exists()


def test_tool_allowlist_and_stricter_reload_permission_are_preserved(setup):
    setup.registration.write_text(setup.registration.read_text().replace('startup_timeout_sec = 20',
        'startup_timeout_sec = 20\nenabled_tools = ["check_gpus", "manage_gpu_job"]')
        .replace('approval_mode = "prompt"', 'approval_mode = "deny"'))
    setup.run()
    server = tomllib.loads(setup.registration.read_text())['mcp_servers']['gpu-cluster-mcp']
    assert server['enabled_tools'] == ['check_gpus', 'manage_gpu_job']
    assert server['tools']['reload_policy']['approval_mode'] == 'deny'


def test_policy_reload_cannot_keep_an_old_automatic_approval(setup):
    setup.registration.write_text(setup.registration.read_text().replace('approval_mode = "prompt"', 'approval_mode = "approve"'))
    setup.run()
    tools = tomllib.loads(setup.registration.read_text())['mcp_servers']['gpu-cluster-mcp']['tools']
    assert tools['reload_policy']['approval_mode'] == 'prompt'


def test_dependency_changes_do_not_touch_another_live_installation(setup, monkeypatch):
    foreign = setup.home / 'gpu-mcp/state/codex/other-installation'
    foreign.mkdir(parents=True)
    info = {'pid': os.getpid()}
    monkeypatch.setattr(upgrade.launcher, 'read_info', lambda root: info)
    monkeypatch.setattr(upgrade.launcher, 'alive', lambda value: True)
    plan = {'root': foreign.with_name('selected'), 'python': sys.executable, 'probe': {'missing': ['mcp']}}
    with pytest.raises(ValueError, match='Another live GPU MCP backend'):
        upgrade.check_shared_interpreters([plan], setup.home)


def test_shell_entrypoint_handles_runtime_path_with_spaces(tmp_path):
    import shutil
    runtime = tmp_path / 'installed runtime'
    runtime.mkdir()
    source = Path(__file__).resolve().parents[1]
    for path in source.glob('gpu_mcp_*.py'):
        shutil.copy2(path, runtime / path.name)
    shutil.copy2(source / 'upgrade.sh', runtime / 'upgrade.sh')
    result = subprocess.run(['bash', str(runtime / 'upgrade.sh'), '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--preview' in result.stdout and '--repo' in result.stdout
    assert '--repair-ssh' not in result.stdout


def test_ssh_recovery_runs_before_optional_legacy_migration_checks(setup, monkeypatch):
    f = setup
    job(f)
    calls = []

    def repair(runtime, repos, **kwargs):
        calls.append((runtime, repos, kwargs))
        return {'repaired': [str(f.repo)], 'refreshed': [], 'deferred': []}

    monkeypatch.setattr(upgrade, 'repair_ssh', repair)
    before = f.registration.read_bytes()
    result = f.run()
    assert calls and result['ssh_repair']['repaired'] == [str(f.repo)]
    assert result['deferred_upgrade'] and before == f.registration.read_bytes()
    assert not (f.home / '.bashrc').exists()


def test_normal_upgrade_automatically_checks_ssh_before_dependencies(setup, monkeypatch):
    f = setup
    calls = []

    def repair(*args, **kwargs):
        calls.append('ssh')
        return {'repaired': [], 'refreshed': [], 'deferred': []}

    def probe(*args):
        assert calls and calls[0] == 'ssh'
        calls.append('dependencies')
        return {'missing': [], 'signature': 'current'}

    monkeypatch.setattr(upgrade, 'repair_ssh', repair)
    monkeypatch.setattr(upgrade, 'probe_python', probe)
    result = f.run()
    assert calls[0] == 'ssh' and 'ssh_repair' in result


def test_ssh_inventory_covers_other_repos_and_live_processes_despite_repo_selection(setup, monkeypatch):
    f = setup
    other = f.repo.parent / 'other-repo'
    (other / '.codex').mkdir(parents=True)
    (other / '.codex/config.toml').write_text(f.registration.read_text()
        .replace(str(f.policy), str(other / 'gpu-mcp.toml')))
    (f.codex_home / 'config.toml').write_text('[projects.' + json.dumps(str(other)) + ']\n')
    live = f.repo.parent / 'live-only'
    (live / '.codex').mkdir(parents=True)
    (live / '.codex/config.toml').write_text(f.registration.read_text()
        .replace(str(f.policy), str(live / 'gpu-mcp.toml')))
    process = {'repo_root':str(live), 'runtime':str(f.runtime), 'shared':False, 'pid':123,
               'resources':{'threads':500, 'sockets':498}}
    monkeypatch.setattr(upgrade, 'running_servers', lambda: [process])
    seen = []

    def repair(runtime, repos, **kwargs):
        seen.append((repos, kwargs))
        return {'repaired': [], 'refreshed': [], 'deferred': []}

    monkeypatch.setattr(upgrade, 'repair_ssh', repair)
    f.run(preview=True)
    assert set(seen[0][0]) == {f.repo, other, live}
    assert seen[0][1]['processes'] == [process]
    assert '--config' in f.registration.read_text()
