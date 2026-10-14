"""A human can inspect legacy/offline jobs without creating a job owner."""
import curses
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

import gpu_mcp_codex_monitor as monitor
from gpu_mcp_codex_state import runtime_root
from test_gpu_mcp_codex_monitor import Screen

pytestmark = pytest.mark.contract


@pytest.fixture
def saved_repo(tmp_path, monkeypatch):
    home, repo, base = tmp_path / 'home', tmp_path / 'repo', tmp_path / 'backends'
    repo.mkdir()
    config = repo / 'gpu-mcp.toml'
    config.write_text('schema_version=1\nrepo_root=' + json.dumps(str(repo)) + '\n'
                      'nodes=["fixture"]\nscript_roots=["."]\nwrite_roots=["."]\n'
                      'output_roots=["logs"]\n')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('the viewer started a process'))
    monkeypatch.setattr(monitor, 'read_info', lambda *a: None)
    record = {'job_id': 'job-20261013T000000Z-fixture', 'script_path': 'jobs/legacy.py',
              'job_title': 'Legacy run', 'job_lifecycle': 'running', 'gpu_index': 1,
              'active_reservation': True, 'output_file': str(repo / 'logs/legacy.log')}
    path = repo / '.gpu_mcp_state/jobs' / record['job_id'] / 'job.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    (repo / 'logs').mkdir()
    (repo / 'logs/legacy.log').write_text('GPU_MCP_PROGRESS {"current": 2, "total": 10}\n')
    return config, base, record, path


def test_saved_legacy_jobs_logs_and_refresh_do_not_reconcile_or_create_state(saved_repo):
    config, base, record, path = saved_repo
    before = path.read_bytes()
    viewer = monitor.connect_viewer(config, base=base)
    snapshot = monitor.request(viewer, '/human/snapshot', human=True)
    assert snapshot['saved_only'] is True and snapshot['jobs'] == [record]
    log = monitor.request(viewer, f"/human/jobs/{record['job_id']}/logs", human=True)
    assert '"current": 2' in log['tail']
    assert path.read_bytes() == before
    assert not base.exists()
    # Changes by the owning legacy server become visible without a new owner.
    record.update(job_lifecycle='succeeded', active_reservation=False)
    path.write_text(json.dumps(record))
    assert monitor.request(viewer, '/human/snapshot', human=True)['jobs'] == [record]
    assert not base.exists()


def test_offline_database_is_read_without_consuming_events_or_replacing_owner(saved_repo):
    config, base, record, path = saved_repo
    root = runtime_root(config, base=base)
    root.mkdir(parents=True)
    database = root / 'jobs.sqlite3'
    authoritative = dict(record, job_lifecycle='succeeded', active_reservation=False,
                         origin_thread_id='original-session')
    with sqlite3.connect(database) as db:
        db.executescript('CREATE TABLE jobs (record TEXT, updated_at TEXT);'
                        'CREATE TABLE events (job_id TEXT, payload TEXT, kind TEXT, state TEXT, updated_at TEXT);')
        db.execute('INSERT INTO jobs VALUES (?,?)', (json.dumps(authoritative), '2026-10-13T01:00:00Z'))
        db.execute('INSERT INTO events VALUES (?,?,?,?,?)',
                   (record['job_id'], '{"message":"Completed"}', 'completion', 'pending', '2026-10-13T01:00:00Z'))
    before = {p: p.read_bytes() for p in (path, database)}
    viewer = monitor.connect_viewer(config, base=base)
    snapshot = monitor.request(viewer, '/human/snapshot', human=True)
    assert snapshot['jobs'] == [authoritative]
    assert snapshot['events'][0]['state'] == 'pending'
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(p.name for p in root.iterdir()) == ['jobs.sqlite3']


def test_saved_case_log_is_selected_from_its_record_and_rejects_other_routes(saved_repo):
    config, base, record, path = saved_repo
    case_log = config.parent / 'logs/case.log'
    case_log.write_text('case-only output\n')
    record.update(job_kind='mps', cases=[{'case_id': 'contrast', 'output_file': str(case_log)}])
    path.write_text(json.dumps(record))
    viewer = monitor.connect_viewer(config, base=base)
    result = monitor.request(viewer, f"/human/jobs/{record['job_id']}/logs?case_id=contrast", human=True)
    assert result['tail'] == 'case-only output\n'
    for route in ('/human/stop-if-idle', '/mcp', '/human/jobs/another/logs'):
        with pytest.raises((PermissionError, ValueError)):
            monitor.request(viewer, route, human=True)
    with pytest.raises(PermissionError):
        monitor.request(viewer, '/human/snapshot')
    with pytest.raises(PermissionError):
        monitor.request(viewer, '/human/snapshot', human=True, payload={})


def test_saved_logs_are_bounded_and_refuse_symlinks_and_outside_paths(saved_repo):
    config, base, record, path = saved_repo
    log = Path(record['output_file'])
    log.write_text('old line\n' * 20000 + 'latest output\n')
    viewer = monitor.connect_viewer(config, base=base)
    route = f"/human/jobs/{record['job_id']}/logs"
    result = monitor.request(viewer, route, human=True)
    assert result['tail'].endswith('latest output\n')
    assert len(result['tail'].encode()) <= 32000 and len(result['tail'].splitlines()) <= 200
    outside = config.parent.parent / 'outside.log'
    outside.write_text('must not appear\n')
    record['output_file'] = str(outside)
    path.write_text(json.dumps(record))
    assert 'error' in monitor.request(viewer, route, human=True)
    symlink = log.with_name('symlink.log')
    symlink.symlink_to(log)
    record['output_file'] = str(symlink)
    path.write_text(json.dumps(record))
    assert 'error' in monitor.request(viewer, route, human=True)


def test_saved_panel_labels_reservations_as_saved_and_does_not_claim_a_live_server(saved_repo, monkeypatch):
    config, base, record, path = saved_repo
    for name in ('curs_set', 'set_escdelay'):
        monkeypatch.setattr(monitor.curses, name, lambda value: None)
    monkeypatch.setattr(monitor.curses, 'has_colors', lambda: False)
    monkeypatch.setattr(monitor.time, 'monotonic', lambda: 0)
    screen = Screen([curses.KEY_RIGHT, 27])
    monitor.view(screen, monitor.connect_viewer(config, base=base))
    assert 'Saved GPU reservations: 1' in screen.frames[0]
    assert 'running (last known)' in screen.frames[0]
    assert 'Saved status' in screen.frames[0]
    assert 'Active GPU reservations' not in screen.frames[0]
    assert path.exists() and not base.exists()


def test_legacy_build_and_mps_protected_records_override_stale_mirrors(saved_repo):
    config, base, record, path = saved_repo
    authoritative = dict(record, repo_root=str(config.parent), job_kind='build',
                         job_lifecycle='succeeded', active_reservation=False)
    root = Path.home() / '.local/state/gpu-mcp'
    protected = root / 'build-jobs' / record['job_id'] / 'job.json'
    protected.parent.mkdir(parents=True)
    protected.write_text(json.dumps(authoritative))
    other = dict(record, job_id='job-20261013T000000Z-other', repo_root='/another/repo', job_kind='mps')
    unrelated = root / 'mps-jobs' / other['job_id'] / 'job.json'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps(other))
    viewer = monitor.connect_viewer(config, base=base)
    assert monitor.request(viewer, '/human/snapshot', human=True)['jobs'] == [authoritative]
    assert json.loads(path.read_text()) == record


def test_live_disconnect_uses_its_original_state_root_without_restarting(saved_repo, monkeypatch):
    config, base, record, path = saved_repo
    root = runtime_root(config, base=base)
    root.mkdir(parents=True)
    info = {'config_path': str(config), 'server_instance_id': 'fixture-owner'}
    monkeypatch.setattr(monitor, 'read_info', lambda *a: info)
    monkeypatch.setattr(monitor, 'alive', lambda *a: True)

    def remote(info, route, **kwargs):
        if route == '/health':
            return {'server_instance_id': 'fixture-owner'}
        raise OSError('backend disconnected')

    monkeypatch.setattr(monitor, 'backend_request', remote)
    viewer = monitor.connect_viewer(config, base=base)
    assert viewer['_viewer_state_root'] == str(root)
    for name in ('curs_set', 'set_escdelay'):
        monkeypatch.setattr(monitor.curses, name, lambda value: None)
    monkeypatch.setattr(monitor.curses, 'has_colors', lambda: False)
    monkeypatch.setattr(monitor.time, 'monotonic', lambda: 0)
    screen = Screen([27])
    monitor.view(screen, viewer)
    assert 'Legacy run' in screen.frames[0] and 'Saved status' in screen.frames[0]
    assert not list(root.iterdir())


def test_live_viewer_cannot_issue_lifecycle_or_notification_mutations(monkeypatch):
    monkeypatch.setattr(monitor, 'backend_request', lambda *a, **k: pytest.fail('a mutation reached the backend'))
    for route in ('/human/stop-if-idle', '/mcp'):
        with pytest.raises(PermissionError):
            monitor.request({}, route, human=True)
    with pytest.raises(PermissionError):
        monitor.request({}, '/human/snapshot', human=True, payload={})
