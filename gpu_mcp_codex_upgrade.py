#!/usr/bin/env python3
"""Upgrade an existing repo's Codex registration without changing GPU policy."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib

from gpu_mcp_builds import control_root
from gpu_mcp_config import load_policy_snapshot
import gpu_mcp_reservations as reservations
from gpu_mcp_policy_approval import verify_policy_approved


def read_record(path):
    reservations._assert_no_symlink_ancestors(path)
    data = reservations.read_json_file_no_follow(path)
    if not isinstance(data, dict):
        raise ValueError(f'job metadata is not an object: {path}')
    return data


def terminal(record, repo):
    job_id = record.get('job_id')
    attempt = record.get('attempt_id') or record.get('active_attempt_id')
    if record.get('job_kind') == 'build':
        # Only the protected build record's reconciled outcome counts. Its
        # repo-writable completion marker is a presence hint, not an outcome.
        if record.get('job_lifecycle') == 'finished' and not record.get('active_reservation'):
            return True
        result = record.get('outcome') or {}
        return (isinstance(result, dict) and bool(record.get('nonce'))
                and record.get('job_lifecycle') in {'succeeded', 'failed'}
                and result.get('nonce') == record['nonce']
                and result.get('job_id') == job_id and result.get('attempt_id') == attempt
                and result.get('terminal_status') in {'success', 'failure'} and bool(result.get('ended_at')))
    try:
        result = read_record(reservations.outcome_record_path(repo, job_id, attempt))
    except (OSError, ValueError, TypeError):
        return False
    return (result.get('job_id') == job_id and result.get('attempt_id') == attempt
            and result.get('host') == record.get('host')
            and result.get('gpu_index') == record.get('gpu_index')
            and result.get('remote_pid') == record.get('remote_pid')
            and result.get('terminal_status') in {'success', 'failure', 'signaled', 'launcher_error'}
            and bool(result.get('ended_at')))


def legacy_jobs(repo, registry):
    blocked, completed, builds = {}, {}, {}
    for path in control_root().glob('job-*/job.json'):
        record = read_record(path)
        if record.get('repo_root') == str(repo) and not record.get('origin_thread_id'):
            builds[record.get('job_id')] = record
    records = list(builds.values())
    for path in reservations.repo_job_state_root(repo).glob('job-*/job.json'):
        record = read_record(path)
        if not record.get('origin_thread_id') and record.get('job_id') not in builds:
            # A build mirror without its protected control record cannot prove
            # that the owning server has finished managing it.
            if record.get('job_kind') == 'build':
                record = dict(record, outcome=None, job_lifecycle='unknown')
            records.append(record)
    for path in registry.glob('*/metadata.json'):
        if not reservations.is_reservation_key(path.parent.name):
            continue
        record = read_record(path)
        if record.get('repo') == str(repo):
            if record.get('job_kind') == 'build':
                proof = builds.get(record.get('job_id'))
                record = dict(record, outcome=(proof or {}).get('outcome'),
                              job_lifecycle=(proof or {}).get('job_lifecycle'), nonce=(proof or {}).get('nonce'))
            records.append(dict(record, reservation_key=path.parent.name))
    for record in records:
        job_id = record.get('job_id')
        item = {'job_id': job_id, 'reservation_key': record.get('reservation_key')}
        if terminal(record, repo):
            if record.get('reservation_key'):
                completed[job_id] = item
        else:
            blocked[job_id] = item
    return list(blocked.values()), [item for key, item in completed.items() if key not in blocked]


def upgrade_plan(config, *, registry_root=None, store_path=None):
    policy = Path(config).expanduser().resolve()
    snapshot = load_policy_snapshot(policy)
    verify_policy_approved(policy, store_path=store_path, expected_hash=snapshot.content_hash)
    repo = snapshot.policy.repo_root
    registration = repo / '.codex/config.toml'
    reservations._assert_no_symlink_ancestors(registration)
    original = registration.read_text()
    server = tomllib.loads(original).get('mcp_servers', {}).get('gpu-cluster-mcp', {})
    args = server.get('args', [])
    if not isinstance(args, list) or args.count('--config') != 1 or args.index('--config') + 1 >= len(args):
        raise ValueError('existing GPU MCP registration must name its repo policy')
    config_arg = args[args.index('--config') + 1]
    if not isinstance(config_arg, str) or not Path(config_arg).is_absolute():
        raise ValueError('GPU policy path in registration must be absolute')
    if Path(config_arg).resolve() != policy:
        raise ValueError('registration points to a different GPU policy')
    candidates = [arg for arg in args if isinstance(arg, str) and Path(arg).name in
                  {'gpu_mcp_server.py', 'gpu_mcp_codex_launcher.py'}]
    if len(candidates) != 1:
        raise ValueError('unsupported launcher; review the existing registration')
    old = candidates[0]
    if not Path(old).is_absolute():
        raise ValueError('installed GPU MCP entrypoint must be absolute')
    launcher = str(Path(old).with_name('gpu_mcp_codex_launcher.py'))
    if not Path(launcher).is_file():
        raise ValueError('update the installed GPU MCP runtime before upgrading this repo')
    mode = 'shared' if Path(old).name == 'gpu_mcp_codex_launcher.py' else 'legacy'
    blocked, completed = [], []
    root = Path(registry_root) if registry_root is not None else reservations.reservation_registry_root()
    if mode == 'legacy':
        blocked, completed = legacy_jobs(repo, root)
    changed = original
    if mode == 'legacy':
        if original.count(old) != 1:
            raise ValueError('legacy path occurs more than once; review registration before changing it')
        changed = original.replace(old, launcher)
        tomllib.loads(changed)
    result = {'status': 'already_shared' if mode == 'shared' else 'blocked' if blocked else 'ready',
              'repo_root': str(repo), 'registration': str(registration),
              'old_entrypoint': old, 'new_entrypoint': launcher,
              'active_legacy_jobs': blocked, 'completed_legacy_bookings': completed,
              'policy_hash': snapshot.content_hash,
              'registration_hash': hashlib.sha256(original.encode()).hexdigest(),
              'activation': 'Reconnect GPU MCP or quit Codex and run codex resume after applying the upgrade.',
              'terminal_activation': '. ~/.config/gpu-mcp/codex-terminal-env.sh (once in an existing shell)'}
    return result, changed


def upgrade(config, *, apply=False, registry_root=None, store_path=None):
    result, changed = upgrade_plan(config, registry_root=registry_root, store_path=store_path)
    if not apply or result['status'] != 'ready':
        return result
    policy = Path(config).expanduser().resolve()
    path = Path(result['registration'])
    current = load_policy_snapshot(policy)
    if current.content_hash != result['policy_hash']:
        raise ValueError('policy changed after planning; upgrade was not applied')
    verify_policy_approved(policy, store_path=store_path, expected_hash=current.content_hash)
    if hashlib.sha256(path.read_bytes()).hexdigest() != result['registration_hash']:
        raise ValueError('registration changed after planning; upgrade was not applied')
    root = Path(registry_root) if registry_root is not None else reservations.reservation_registry_root()
    blocked, completed = legacy_jobs(current.policy.repo_root, root)
    if blocked:
        return dict(result, status='blocked', active_legacy_jobs=blocked, completed_legacy_bookings=completed)
    backup = Path(tempfile.mkdtemp(prefix='gpu_mcp_codex_upgrade_'))
    shutil.copy2(path, backup / 'config.toml')
    fd, temporary = tempfile.mkstemp(prefix='.gpu-mcp-upgrade-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(changed)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    result = dict(result, status='upgraded', backup_directory=str(backup))
    (backup / 'upgrade.json').write_text(json.dumps(dict(result, at=datetime.now(timezone.utc).isoformat()), indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = upgrade(args.config, apply=args.apply)
    except (OSError, ValueError, IndexError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, indent=2))
    return 1 if result['status'] == 'blocked' else 0


if __name__ == '__main__':
    raise SystemExit(main())
