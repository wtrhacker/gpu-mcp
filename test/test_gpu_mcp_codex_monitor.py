"""Human page navigation, retained selection and read-only inspection."""
import curses
from copy import deepcopy
import re

import pytest

import gpu_mcp_codex_monitor as monitor

pytestmark = pytest.mark.contract


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.frames = []
        self.lines = {}

    def getmaxyx(self):
        return 20, 240

    def timeout(self, value):
        pass

    def keypad(self, value):
        pass

    def erase(self):
        self.lines = {}

    def addnstr(self, y, x, text, width, style=0):
        self.lines[y] = text[:width]

    def refresh(self):
        self.frames.append('\n'.join(self.lines.values()))

    def getch(self):
        return next(self.keys)

    def get_wch(self):
        key = self.getch()
        return chr(key) if isinstance(key, int) and 0 <= key < 256 else key


@pytest.fixture
def human_view(monkeypatch):
    monkeypatch.setattr(monitor.curses, 'curs_set', lambda value: None)
    monkeypatch.setattr(monitor.curses, 'set_escdelay', lambda value: None)
    monkeypatch.setattr(monitor.curses, 'has_colors', lambda: False)
    monkeypatch.setattr(monitor.time, 'monotonic', lambda: 0)
    snapshot = {
        'repo_root': '/fixture', 'health': {},
        'jobs': [{'job_id': f'job-{label}', 'active_attempt_id': f'attempt-{label}',
                  'script_path': f'jobs/{label}.py', 'job_lifecycle': 'running',
                  'origin_thread_id': f'owner-{label}'} for label in ('alpha', 'beta')],
        'events': [{'job_id': f'job-{label}', 'updated_at': '2026-10-08T21:00:00Z',
                    'kind': 'milestone', 'state': 'accepted',
                    'payload': {'message': f'{label} milestone'}} for label in ('alpha', 'beta')],
    }
    calls = []

    def request(info, path, **kwargs):
        assert kwargs.get('human') is True
        assert 'payload' not in kwargs, 'navigation must not mutate the backend'
        calls.append(path)
        if path == '/human/snapshot':
            return deepcopy(snapshot)
        assert path.startswith('/human/jobs/') and path.endswith('/logs')
        return {'tail': '\n'.join(f'{path} log line {i}' for i in range(30))}

    monkeypatch.setattr(monitor, 'request', request)
    return snapshot, calls


def test_left_right_browse_pages_and_retain_the_selected_job(human_view):
    _, calls = human_view
    screen = Screen([curses.KEY_DOWN, curses.KEY_RIGHT, curses.KEY_RIGHT,
                     curses.KEY_RIGHT, curses.KEY_LEFT, curses.KEY_LEFT,
                     curses.KEY_LEFT, 27])
    monitor.view(screen, {})
    pages = [re.search(r'\[(Jobs|Details|Logs|Events)\]', frame)[1] for frame in screen.frames]
    assert pages == ['Jobs', 'Jobs', 'Details', 'Logs', 'Events', 'Logs', 'Details', 'Jobs']
    assert 'job-beta' in screen.frames[2] and 'job-alpha' not in screen.frames[2]
    assert 'beta milestone' in screen.frames[4]
    assert 'alpha milestone' not in screen.frames[4]
    assert calls.count('/human/snapshot') == 1
    assert set(calls) == {'/human/snapshot', '/human/jobs/job-beta/logs'}


def test_logs_scroll_refresh_and_reordered_snapshot_keep_the_owner(human_view, monkeypatch):
    snapshot, calls = human_view
    original = monitor.request
    reads = 0

    def request(info, path, **kwargs):
        nonlocal reads
        result = original(info, path, **kwargs)
        if path.endswith('/logs'):
            reads += 1
            if reads == 1:
                snapshot['jobs'].reverse()
        return result

    monkeypatch.setattr(monitor, 'request', request)
    screen = Screen([curses.KEY_DOWN, ord('l'), curses.KEY_HOME, curses.KEY_DOWN,
                     curses.KEY_NPAGE, ord('r'), 27])
    monitor.view(screen, {})
    assert 'log line 0' in screen.frames[3]
    assert 'log line 1' in screen.frames[4] and 'log line 0\n' not in screen.frames[4]
    assert 'log line 14' in screen.frames[5]
    assert all('job-alpha/logs' not in frame for frame in screen.frames[2:])
    assert calls.count('/human/jobs/job-beta/logs') == 2
    assert calls.count('/human/snapshot') == 2


def test_empty_repo_pages_and_back_do_not_read_a_job(human_view):
    snapshot, calls = human_view
    snapshot['jobs'] = []
    snapshot['events'] = []
    screen = Screen([curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_RIGHT, ord('b')])
    assert monitor.view(screen, {}, chooser=True) == 'chooser'
    assert all('No job selected.' in frame for frame in screen.frames[1:3])
    assert 'No events recorded.' in screen.frames[3]
    assert calls == ['/human/snapshot']


def test_page_edges_wrap_and_escape_closes_directly(human_view):
    screen = Screen([curses.KEY_LEFT, curses.KEY_RIGHT, 27])
    assert monitor.view(screen, {}) is None
    assert '[Events]' in screen.frames[1]
    assert '[Jobs]' in screen.frames[2]


def test_active_reservations_filter_shows_title_code_and_sweep_arguments(human_view):
    snapshot, calls = human_view
    snapshot['jobs'][0].update(job_title='Completed baseline', gpu_index=0, active_reservation=False)
    snapshot['jobs'][1].update(job_title='Learning rate sweep', gpu_index=1, active_reservation=True,
                               args=['--lr', '0.01', '--seed', '3'])
    screen = Screen([ord('v'), curses.KEY_RIGHT, curses.KEY_LEFT, ord('v'), curses.KEY_UP, 27])
    monitor.view(screen, {})
    assert 'Active GPU reservations: 1' in screen.frames[0]
    assert 'Learning rate sweep' in screen.frames[1]
    assert 'jobs/beta.py' in screen.frames[1]
    assert '--lr 0.01 --seed 3' in screen.frames[1]
    assert 'Reservation: held' in screen.frames[1]
    assert 'Completed baseline' not in screen.frames[1]
    assert 'job-beta' in screen.frames[2]
    assert 'Completed baseline' in screen.frames[5]
    assert calls == ['/human/snapshot']


def test_history_search_edits_as_text_and_escape_cancels_without_closing(human_view):
    snapshot, calls = human_view
    snapshot['jobs'][0].update(job_title='Baseline fit', job_role='smoke', job_lifecycle='succeeded')
    snapshot['jobs'][1].update(job_title='β rate sweep', job_role='main', job_lifecycle='failed')
    screen = Screen([ord('/'), *map(ord, 'SMOKE'), 10, curses.KEY_RIGHT,
                     ord('/'), 21, *map(ord, 'qfailed'), 27, curses.KEY_LEFT, 27])
    monitor.view(screen, {})
    committed = next(frame for frame in screen.frames if 'Search: SMOKE' in frame and 'Baseline fit' in frame)
    assert 'β rate sweep' not in committed
    assert 'job-alpha' in screen.frames[8]
    assert 'Search: SMOKE' in screen.frames[-1] and '[Jobs]' in screen.frames[-1]
    assert calls == ['/human/snapshot'], 'search text must not execute shortcuts or read logs'


def test_unicode_search_and_arguments_stay_out_of_other_fields(human_view):
    snapshot, calls = human_view
    snapshot['jobs'][1].update(job_title='β rate sweep', job_role='smoke',
                               args=['--config', 'x' * 300])
    screen = Screen([ord('/'), 'β', 10, 27])
    monitor.view(screen, {})
    frame = screen.frames[-1]
    assert 'β rate sweep' in frame and 'alpha.py' not in frame
    assert '[Smoke]' in frame
    argument_lines = [line for line in frame.splitlines() if '--config' in line or 'xxx' in line]
    assert len(argument_lines) >= 2
    assert all('Launched' not in line and 'Progress' not in line for line in argument_lines)
    assert 'Check-in' not in frame and 'Result scan' not in frame and 'scan not recorded' not in frame
    assert calls == ['/human/snapshot']


def test_real_log_counters_are_display_only_and_not_guessed_from_time(human_view, monkeypatch):
    snapshot, calls = human_view
    job = snapshot['jobs'][0]
    job.update(output_file='/fixture/logs/task.log', job_role='smoke', expected_duration_sec=10)
    original = monitor.request

    def request(info, path, **kwargs):
        if path.endswith('/logs'):
            assert kwargs['human'] is True
            calls.append(path)
            return {'tail': 'GPU_MCP_PROGRESS {"current": 2, "total": 10}\n'}
        return original(info, path, **kwargs)

    monkeypatch.setattr(monitor, 'request', request)
    before = deepcopy(snapshot['jobs'])
    screen = Screen([27])
    monitor.view(screen, {})
    assert '2/10' in screen.frames[0] and '█' in screen.frames[0]
    assert '[Smoke]' in screen.frames[0]
    assert snapshot['jobs'] == before
    assert calls == ['/human/snapshot', '/human/jobs/job-alpha/logs']
