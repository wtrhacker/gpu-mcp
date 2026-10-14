"""Read saved human-view data without starting a server or reconciling jobs."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import parse_qs, urlsplit

from gpu_mcp_config import load_policy
import gpu_mcp_reservations as reservations


class SavedViewer:
    def __init__(self, config, root, *, notice='Saved status; live checks unavailable.'):
        self.config, self.root = Path(config).resolve(), Path(root).expanduser().resolve()
        self.notice = notice
        self._cache = {}

    def _record(self, path):
        """Reuse unchanged mirrors; never open a pipe or follow a job symlink."""
        try:
            if path.parent.is_symlink():
                return None
            status = path.lstat()
            stamp = (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)
            if not stat.S_ISREG(status.st_mode) or status.st_size > 4 * 1024 * 1024:
                return None
            cached = self._cache.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            with os.fdopen(os.open(path, flags), 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None
                data = stream.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                return None
            record = json.loads(data)
            if not isinstance(record, dict) or record.get('job_id') != path.parent.name:
                return None
            self._cache[path] = (stamp, record)
            return record
        except (OSError, ValueError):
            return None

    def _database(self):
        path = self.root / 'jobs.sqlite3'
        jobs, events = [], []
        if not path.is_file():
            return jobs, events
        reservations._assert_no_symlink_ancestors(path)
        # JobStore's constructor creates directories/schema and changes modes.
        # Open SQLite directly in read-only mode instead.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            for row in db.execute('SELECT record FROM jobs ORDER BY updated_at DESC'):
                try:
                    record = json.loads(row['record'])
                    if isinstance(record, dict) and reservations.is_job_id(record.get('job_id')):
                        jobs.append(record)
                except ValueError:
                    continue
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'events' in tables:
                for row in db.execute('SELECT * FROM events ORDER BY updated_at DESC LIMIT 500'):
                    try:
                        event = dict(row, payload=json.loads(row['payload']))
                        if isinstance(event['payload'], dict):
                            events.append(event)
                    except ValueError:
                        continue
        return jobs, list(reversed(events))

    def snapshot(self):
        repo, jobs, events, seen = self.config.parent, {}, [], set()
        roots = [(repo / '.gpu_mcp_state/jobs', False)]
        roots += [(Path.home() / '.local/state/gpu-mcp' / name, True)
                  for name in ('build-jobs', 'mps-jobs')]
        for root, protected in roots:
            try:
                reservations._assert_no_symlink_ancestors(root)
                for path in root.glob('job-*/job.json'):
                    seen.add(path)
                    record = self._record(path)
                    if (record and reservations.is_job_id(record.get('job_id'))
                            and record.get('repo_root', str(repo)) == str(repo)
                            and (not protected or record.get('repo_root') == str(repo))):
                        jobs[record['job_id']] = record
            except OSError:
                continue
        self._cache = {path: value for path, value in self._cache.items() if path in seen}
        notice = self.notice
        try:
            records, events = self._database()
            for record in records:
                if record.get('repo_root', str(repo)) == str(repo):
                    jobs[record['job_id']] = record
        except (OSError, sqlite3.Error) as exc:
            notice += ' Saved database unavailable: ' + str(exc)
        return {'repo_root': str(repo), 'saved_only': True, 'notice': notice,
                'jobs': sorted(jobs.values(), key=lambda r: r['job_id'], reverse=True),
                'events': events, 'health': {}, 'monitoring': {}}

    def request(self, route):
        parsed = urlsplit(route)
        if parsed.path == '/human/snapshot' and not parsed.query:
            return self.snapshot()
        parts = parsed.path.split('/')
        if len(parts) != 5 or parts[:3] != ['', 'human', 'jobs'] or parts[4] != 'logs':
            raise PermissionError('the saved viewer only reads snapshots and recorded logs')
        record = next((r for r in self.snapshot()['jobs'] if r['job_id'] == parts[3]), None)
        if record is None:
            raise ValueError('job unavailable')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - {'case_id'} or len(query.get('case_id', [])) > 1:
            raise ValueError('invalid log query')
        case_id = query.get('case_id', [None])[0]
        if case_id is not None:
            case = next((c for c in record.get('cases', []) if c.get('case_id') == case_id), None)
            if record.get('job_kind') != 'mps' or case is None:
                raise ValueError('case unavailable')
            record = case
        try:
            if not record.get('output_file'):
                raise ValueError('no saved log path')
            path = Path(record['output_file']).expanduser()
            if not path.is_absolute():
                path = self.config.parent / path
            reservations._assert_no_symlink_ancestors(path)
            if not any(path.resolve().is_relative_to(root) for root in load_policy(self.config).output_roots):
                raise ValueError('saved log is outside the policy output roots')
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            with os.fdopen(os.open(path, flags), 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError('saved log is not a regular file')
                start = max(0, stream.seek(0, os.SEEK_END) - 32000)
                stream.seek(start)
                data = stream.read(32000)
            if start and b'\n' in data:
                data = data.split(b'\n', 1)[1]
            return {'tail': ''.join(data.decode(errors='replace').splitlines(keepends=True)[-200:])}
        except (OSError, ValueError) as exc:
            return {'error': str(exc)}
