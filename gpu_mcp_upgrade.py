#!/usr/bin/env python3
"""Update existing Codex installations and their monitor with one command."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib

import gpu_mcp_codex_launcher as launcher
import gpu_mcp_codex_terminal_setup as terminal_setup
import gpu_mcp_codex_upgrade as migration
from gpu_mcp_codex_state import private_directory, runtime_root
from gpu_mcp_policy_approval import default_store_path
from gpu_mcp_ssh_recovery import repair as repair_ssh, running_servers

TOOLS = ('run_python_on_gpu', 'check_gpus', 'kill_gpu_process', 'check_gpu_processes',
         'cluster_info', 'manage_gpu_job', 'list_gpu_reservations', 'list_gpu_jobs',
         'build_python_artifact', 'preview_policy_reload', 'reject_policy_reload', 'reload_policy')
DEPENDENCIES = ('mcp>=1.28,<2', 'fabric', 'paramiko', 'uvicorn', 'websocket-client')
TERMINAL = {'succeeded', 'failed', 'finished', 'process_gone_unknown_outcome'}
SERVER = ('mcp_servers', 'gpu-cluster-mcp')


class LegacyWorkActive(ValueError):
    """Code cleanup can be installed while registration migration must wait."""


def read_toml(path):
    migration.reservations._assert_no_symlink_ancestors(path)
    return tomllib.loads(path.read_text())


def discovery(runtime, home, codex_home, store, *, all_installations=False):
    """Use known registrations; never crawl a home directory or network mount."""
    candidates = {Path.cwd()}
    if store.exists():
        candidates.update(Path(p).parent for p in migration.read_record(store) if Path(p).is_absolute())
    config = codex_home / 'config.toml'
    if config.is_file():
        candidates.update(Path(p) for p in read_toml(config).get('projects', {}) if Path(p).is_absolute())
    for path in (home / 'gpu-mcp/state/codex').glob('*/backend.json'):
        info = migration.read_record(path)
        if isinstance(info.get('config_path'), str):
            candidates.add(Path(info['config_path']).parent)
    found = []
    for repo in sorted(candidates):
        path = repo / '.codex/config.toml'
        if not path.is_file():
            continue
        server = read_toml(path).get('mcp_servers', {}).get('gpu-cluster-mcp', {})
        entries = [p for p in server.get('args', []) if isinstance(p, str)
                   and Path(p).name in {'gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'}]
        if (server.get('enabled', True) and len(entries) == 1 and Path(entries[0]).is_absolute()
                and (all_installations or Path(entries[0]).parent.resolve() == runtime)):
            args = server.get('args', [])
            if '--config' in args and args.index('--config') + 1 < len(args):
                policy = Path(args[args.index('--config') + 1])
                # A copied runtime can retain a development registration for
                # its source repo. It is not another research installation.
                if policy.is_absolute() and policy.parent.resolve() != repo.resolve():
                    continue
            found.append(repo.resolve())
    return sorted(set(found))


def table_path(line):
    if not re.match(r'^\s*\[(?!\[)', line):
        return None
    try:
        value = tomllib.loads(line + '\n__upgrade_marker__ = true\n')
        path = []
        while '__upgrade_marker__' not in value:
            if len(value) != 1:
                return None
            key, value = next(iter(value.items()))
            path.append(key)
        return tuple(path)
    except (ValueError, TypeError):
        return None


def set_mode(text, tool, mode):
    target = SERVER + ('tools', tool)
    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if table_path(line) != target:
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
        for i in range(start + 1, end):
            if re.match(r'^\s*approval_mode\s*=', lines[i]):
                lines[i] = f'approval_mode = "{mode}"\n'
                return ''.join(lines)
        lines.insert(start + 1, f'approval_mode = "{mode}"\n')
        return ''.join(lines)
    return text.rstrip() + f'\n\n[mcp_servers.gpu-cluster-mcp.tools.{tool}]\napproval_mode = "{mode}"\n'


def registration_plan(repo, runtime, store, home):
    registration = repo / '.codex/config.toml'
    original = read_toml(registration)
    server = original.get('mcp_servers', {}).get('gpu-cluster-mcp', {})
    args = server.get('args', [])
    if server.get('enabled', True) is False:
        raise ValueError(f'{repo}: GPU MCP is disabled; its settings were preserved')
    if '--config' not in args or args.index('--config') + 1 == len(args):
        raise ValueError(f'{repo}: registration does not name a policy')
    policy = Path(args[args.index('--config') + 1])
    plan, changed = migration.upgrade_plan(policy, store_path=store)
    if Path(plan['repo_root']) != repo:
        raise ValueError(f'{repo}: registration names another repository')
    if Path(plan['old_entrypoint']).parent.resolve() != runtime:
        raise ValueError(f'{repo}: update its installed folder, {Path(plan["old_entrypoint"]).parent}, then run upgrade.sh there')
    if plan['active_legacy_jobs']:
        raise LegacyWorkActive(f'{repo}: finish its legacy jobs, then rerun upgrade.sh')
    expected = copy.deepcopy(tomllib.loads(changed))
    tools = expected['mcp_servers']['gpu-cluster-mcp'].setdefault('tools', {})
    for name in TOOLS:
        # Keep explicit prompt/deny settings and tool allowlists. Policy reload
        # may never bypass the existing human approval boundary.
        current = tools.get(name, {}).get('approval_mode')
        desired = 'prompt' if name == 'reload_policy' else 'approve'
        if current is None or (name == 'reload_policy' and current == 'approve'):
            changed = set_mode(changed, name, desired)
            tools.setdefault(name, {})['approval_mode'] = desired
    if tomllib.loads(changed) != expected:
        raise ValueError(f'{repo}: unsupported inline registration; no settings changed')
    python = server.get('command', '')
    if not Path(python).is_absolute() or not Path(python).is_file() or not Path(python).name.startswith('python'):
        raise ValueError(f'{repo}: registration must use an existing absolute Python executable')
    base = home / 'gpu-mcp/state/codex'
    if '--state-base' in args:
        base = Path(args[args.index('--state-base') + 1])
        if not base.is_absolute():
            raise ValueError(f'{repo}: --state-base must be absolute')
    return dict(plan, config=policy, text=changed, server=server, python=python,
                root=runtime_root(policy, base=base), changed=changed != registration.read_text())


def probe_python(plan, runtime):
    program = '''import importlib, importlib.metadata, json
missing=[]
for module, package in [('mcp.server.fastmcp','mcp'), ('fabric','fabric'), ('paramiko','paramiko'), ('uvicorn','uvicorn'), ('websocket','websocket-client')]:
    try:
        importlib.import_module(module)
        if package=='mcp':
            version=importlib.metadata.version(package).split('.')
            assert int(version[0])==1 and int(version[1])>=28
    except (ImportError, AssertionError, ValueError, importlib.metadata.PackageNotFoundError): missing.append(package)
from gpu_mcp_codex_launcher import runtime_signature
print(json.dumps({'missing':missing,'signature':runtime_signature()}))
'''
    env = dict(os.environ, **plan['server'].get('env', {}), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([plan['python'], '-B', '-c', program], cwd=runtime, env=env,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f'{plan["repo_root"]}: Python dependency check failed: {result.stderr.strip()[-1500:]}')
    return json.loads(result.stdout)


def backend_to_refresh(plan, probe):
    info = launcher.read_info(plan['root'])
    if not launcher.alive(info):
        return None
    if info.get('config_path') != str(plan['config']):
        raise ValueError(f'{plan["repo_root"]}: backend policy identity differs; no refresh attempted')
    if (not probe['missing'] and info.get('code_hash') == launcher.code_hash()
            and info.get('runtime_signature') == probe['signature']):
        return None
    snapshot = launcher.request(info, '/human/snapshot', human=True, timeout=10)
    if snapshot.get('server_instance_id') != info.get('server_instance_id'):
        raise ValueError('backend identity changed during upgrade; rerun')
    active = [j for j in snapshot['jobs'] if j.get('server_instance_id') == info['server_instance_id']
              and (j.get('job_lifecycle') not in TERMINAL or j.get('active_reservation', bool(j.get('reservation_key'))))]
    if active:
        raise ValueError(f'{plan["repo_root"]}: finish {len(active)} active job(s), then rerun upgrade.sh')
    return info


def stop_idle(info):
    result = launcher.request(info, '/human/stop-if-idle', payload={}, human=True, timeout=10)
    if result.get('status') != 'stopping':
        raise ValueError('GPU MCP became busy; finish its jobs/requests, then rerun. ' + result.get('reason', ''))
    deadline = time.monotonic() + 15
    while launcher.alive(info) and time.monotonic() < deadline:
        time.sleep(.1)
    if launcher.alive(info):
        raise ValueError('GPU MCP has not finished stopping; no process was killed. Rerun shortly.')


def check_unchanged(plans, runtime, store, home):
    for plan in plans:
        fresh = registration_plan(Path(plan['repo_root']), runtime, store, home)
        if (fresh['registration_hash'], fresh['policy_hash']) != (plan['registration_hash'], plan['policy_hash']):
            raise ValueError('Registration or policy changed during upgrade; rerun before making changes')


def check_shared_interpreters(plans, home):
    needed = {Path(p['python']).resolve() for p in plans if p['probe']['missing']}
    if not needed:
        return
    selected = {p['root'] for p in plans}
    bases = {home / 'gpu-mcp/state/codex'} | {p.parent for p in selected}
    for base in bases:
        for root in base.iterdir() if base.is_dir() else ():
            if root in selected:
                continue
            info = launcher.read_info(root)
            if not launcher.alive(info):
                continue
            try:
                python = Path(f'/proc/{info["pid"]}/exe').resolve(strict=True)
            except OSError as exc:
                raise ValueError('Cannot verify another live GPU MCP interpreter; dependencies were not changed') from exc
            if python in needed:
                raise ValueError('Another live GPU MCP backend shares this Python environment: '
                                 + str(info.get('config_path', root)) + '. '
                                 'Finish its work before installing dependencies. Run without --repo '
                                 'to include other repositories using this installation.')


def upgrade(runtime, *, repos=(), home=None, codex_home=None, store=None, preview=False,
            shell=None, with_tmux=None):
    runtime = Path(runtime).resolve()
    home = Path(home or Path.home()).resolve()
    codex_home = Path(codex_home or os.environ.get('CODEX_HOME', home / '.codex')).resolve()
    store = Path(store or default_store_path())
    processes = running_servers()
    selected = [Path(p).expanduser().resolve() for p in repos]
    repos = selected or discovery(runtime, home, codex_home, store)
    if not selected:
        repos = sorted(set(repos) | {Path(p['repo_root']) for p in processes
                       if Path(p['runtime']).resolve() == runtime
                       and (Path(p['repo_root']) / '.codex/config.toml').is_file()})
    # --repo limits configuration migrations. The automatic SSH check still
    # inventories every known registration and running MCP controller.
    repair_repos = set(discovery(runtime, home, codex_home, store, all_installations=True)) | set(repos)
    repair_repos.update(Path(p['repo_root']) for p in processes
                        if (Path(p['repo_root']) / '.codex/config.toml').is_file())
    print(f'Checking GPU MCP usage: {len(repair_repos)} known repositories, '
          f'{len(processes)} running control processes.', flush=True)
    repairs = repair_ssh(runtime, sorted(repair_repos), home=home, preview=preview, processes=processes)
    if not repos:
        raise ValueError('No existing GPU MCP registrations point to this folder. '
                         'The running-process inventory above still identifies older sessions and other copies. '
                         'Run this script from the installed copy.')
    plans, blocked, deferred = [], [], []
    for repo in sorted(set(repos)):
        try:
            plans.append(registration_plan(repo, runtime, store, home))
        except LegacyWorkActive as exc:
            deferred.append(str(exc))
        except (OSError, ValueError, KeyError, IndexError) as exc:
            blocked.append(str(exc))
    if blocked:
        raise ValueError('\n'.join(blocked) + '\nNo registrations or dependencies changed. '
                         'Finish the reported work, or use --repo to upgrade an idle project.')
    if deferred:
        print(('Preview: registration migrations must wait.' if preview else
               'Upgrade incomplete: registration migrations must wait.') + '\n' + '\n'.join(deferred), flush=True)
        print('Finish active legacy work, then quit/resume those sessions to load the fixed SSH helper. '
              'Rerun upgrade.sh afterward for the remaining migrations.')
        return dict(status='blocked', ssh_repair=repairs, deferred_upgrade=deferred, changed_files=[], preview=preview)
    probes = {}
    for plan in plans:
        key = (plan['python'], json.dumps(plan['server'].get('env', {}), sort_keys=True))
        if key not in probes:
            probes[key] = probe_python(plan, runtime)
        plan['probe'] = probes[key]
        plan['backend'] = backend_to_refresh(plan, plan['probe'])
    check_shared_interpreters(plans, home)
    shell = shell or terminal_setup.shell_name()
    with_tmux = terminal_setup.tmux_available() if with_tmux is None else with_tmux
    terminal_setup.install(home, runtime, plans[0]['python'], shell, preview=True,
                           with_tmux=with_tmux, activate_tmux=True)
    for plan in plans:
        print(f'{plan["repo_root"]}: ' + ('update registration' if plan['changed'] else 'registration current')
              + ('; refresh idle backend' if plan['backend'] else '')
              + ('; install Python dependencies' if plan['probe']['missing'] else ''), flush=True)
    if preview:
        print('Preview only. Ctrl+G setup will also be checked and installed.')
        return {'preview': True, 'repos': len(plans)}

    with ExitStack() as stack:
        for plan in plans:
            private_directory(plan['root'])
            fd = os.open(plan['root'] / 'startup.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            lock = stack.enter_context(os.fdopen(fd, 'w'))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        check_unchanged(plans, runtime, store, home)
        check_shared_interpreters(plans, home)
        for plan in plans:
            plan['backend'] = backend_to_refresh(plan, plan['probe'])
        for plan in plans:
            if plan['backend']:
                stop_idle(plan['backend'])
        installed = set()
        for plan in plans:
            if plan['probe']['missing'] and plan['python'] not in installed:
                subprocess.run([plan['python'], '-m', 'pip', 'install', '--disable-pip-version-check', *DEPENDENCIES],
                               cwd=runtime, env=dict(os.environ, **plan['server'].get('env', {})), check=True)
                if probe_python(plan, runtime)['missing']:
                    raise ValueError('Dependency installation did not pass the import check')
                installed.add(plan['python'])
        check_unchanged(plans, runtime, store, home)
        changes = {Path(p['registration']): p['text'] for p in plans if p['changed']}
        result = terminal_setup.write_changes(changes) if changes else {'changed_files': []}
        terminal_result = terminal_setup.install(home, runtime, plans[0]['python'], shell,
                                                 with_tmux=with_tmux, activate_tmux=True)
    for report in (result, terminal_result):
        if report.get('backup_directory'):
            print('Backup: ' + report['backup_directory'])
    print(terminal_result['activation'])
    pending = [p for p in processes if not p['shared'] and Path(p['runtime']).resolve() == runtime
               and Path(p['repo_root']).resolve() in repos]
    if pending:
        print('Runtime updated; activation incomplete. Old per-session controllers are still running. '
              'Finish their jobs, then quit/resume those conversations. Existing configurations will use the shared backend.')
    else:
        print('Upgrade complete. Quit and resume existing Codex conversations once to refresh their MCP tools.')
    print('New sessions use the update automatically. The Codex daemon was not restarted or reconfigured.')
    return dict(result, terminal=terminal_result, ssh_repair=repairs, pending_legacy_sessions=pending)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', action='store_true', help='show changes without writing, installing or stopping anything')
    parser.add_argument('--repo', type=Path, action='append', default=[], help='limit upgrade to this existing repository; repeatable')
    args = parser.parse_args(argv)
    try:
        result = upgrade(Path(__file__).resolve().parent, repos=args.repo, preview=args.preview)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        print('Upgrade stopped: ' + str(exc), file=sys.stderr)
        return 1
    return 2 if result.get('deferred_upgrade') or result.get('pending_legacy_sessions') else 0


if __name__ == '__main__':
    raise SystemExit(main())
