#!/usr/bin/env python3
"""Read-only human terminal viewer. Data never passes through an agent tool."""
from __future__ import annotations

import argparse
import curses
import json
from pathlib import Path
import sys
import textwrap
import time
import tomllib
from urllib.parse import urlencode, urlsplit

from gpu_mcp_codex_launcher import alive, read_info, request as backend_request
from gpu_mcp_codex_state import runtime_root
from gpu_mcp_monitor_saved import SavedViewer
from gpu_mcp_monitor_display import STATUS_COLORS, matches_job, messages, parse_log_progress, progress_text, safe_text, task_row


def request(info, path, *, payload=None, human=False, timeout=120):
    parsed = urlsplit(path)
    parts = parsed.path.split('/')
    log_route = len(parts) == 5 and parts[:3] == ['', 'human', 'jobs'] and parts[4] == 'logs'
    if (payload is not None or parsed.scheme or parsed.netloc
            or not (parsed.path in ('/health', '/human/snapshot') or log_route)):
        raise PermissionError('the viewer only reads health, snapshots and recorded logs')
    if parsed.path != '/health' and not human:
        raise PermissionError('monitor data is human-only')
    if isinstance(info, SavedViewer):
        if not human:
            raise PermissionError('saved monitor data is read-only and human-only')
        return info.request(path)
    return backend_request(info, path, payload=payload, human=human, timeout=timeout)


def connect_viewer(config, *, base=None):
    """Use a live backend or saved records; viewing never starts a server."""
    config = Path(config).expanduser().resolve()
    if base is None:
        try:
            with (config.parent / '.codex/config.toml').open('rb') as stream:
                args = tomllib.load(stream).get('mcp_servers', {}).get('gpu-cluster-mcp', {}).get('args', [])
            if '--state-base' in args:
                registered_base = args[args.index('--state-base') + 1]
                if isinstance(registered_base, str) and Path(registered_base).is_absolute():
                    base = registered_base
        except (OSError, ValueError, IndexError, AttributeError, TypeError):
            pass
    root = runtime_root(config, base=base)
    info = read_info(root)
    if alive(info):
        try:
            if info.get('config_path') != str(config):
                raise ValueError('the live backend belongs to another policy path')
            health = request(info, '/health', timeout=3)
            if health.get('server_instance_id') != info.get('server_instance_id'):
                raise ValueError('the live backend identity changed')
            return dict(info, _viewer_state_root=str(root))
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            return SavedViewer(config, root, notice='Saved status; backend unavailable: ' + str(exc))
    return SavedViewer(config, root)


def view(screen, info, *, chooser=False):
    curses.curs_set(0)
    curses.set_escdelay(25)
    screen.timeout(200)
    screen.keypad(True)
    colors = {}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for i, (name, color) in enumerate({"gray": curses.COLOR_WHITE, "cyan": curses.COLOR_CYAN,
                "green": curses.COLOR_GREEN, "red": curses.COLOR_RED, "yellow": curses.COLOR_YELLOW}.items(), 1):
            curses.init_pair(i, color, -1)
            colors[name] = curses.color_pair(i)
    pages = ('jobs', 'details', 'logs', 'events')
    snapshot, selection, mode, logs = {}, None, 'jobs', ''
    case_selection, case_parent = None, None
    offsets = {'details': 0, 'logs': None, 'events': None, 'runs': 0}
    logs_for = None
    reservations_only = False
    query, draft, cursor, searching, search_anchor = '', '', 0, False, None
    progress_cache = {}
    refresh_at, error = 0, ""

    def line(y, text, style=0):
        height, width = screen.getmaxyx()
        if 0 <= y < height - 1 and width > 1:
            try:
                screen.addnstr(y, 0, safe_text(text).replace("\n", " "), width - 1, style)
            except curses.error:
                pass  # Resizing/wide text must not close the terminal handoff.

    while True:
        if time.monotonic() >= refresh_at:
            try:
                snapshot = request(info, "/human/snapshot", human=True, timeout=3)
                error = ""
            except Exception as exc:
                if not isinstance(info, SavedViewer) and info.get('config_path'):
                    root = info.get('_viewer_state_root') or runtime_root(info['config_path'])
                    info = SavedViewer(info['config_path'], root)
                    snapshot = request(info, '/human/snapshot', human=True)
                    error = ''
                else:
                    error = 'Monitor disconnected: ' + str(exc)
            refresh_at = time.monotonic() + 1
        all_jobs = snapshot.get('jobs', [])
        active_reservations = [r for r in all_jobs if r.get('gpu_index') is not None and r.get('active_reservation')]
        candidates = active_reservations if reservations_only else all_jobs
        jobs = [r for r in candidates if matches_job(r, draft if searching else query)]
        ids = [r["job_id"] for r in jobs]
        if selection not in ids:
            selection = ids[0] if ids else None
        index = ids.index(selection) if selection else 0
        record = next((r for r in jobs if r['job_id'] == selection), None)
        pages = ('jobs', 'runs', 'details', 'logs', 'events') if record and record.get('job_kind') == 'mps' else ('jobs', 'details', 'logs', 'events')
        if mode not in pages:
            mode = 'jobs'
        if case_parent != selection:
            case_selection, case_parent = None, selection
        cases = record.get('cases', []) if record else []
        case_ids = [c['case_id'] for c in cases]
        if case_selection not in case_ids:
            case_selection = None
        selected_case = next((c for c in cases if c['case_id'] == case_selection), None)
        identity = (selection, record.get('active_attempt_id'), case_selection,
                    selected_case.get('attempt_id') if selected_case else None) if record else None
        if mode == 'logs' and identity != logs_for:
            try:
                suffix = '?' + urlencode({'case_id': case_selection}) if case_selection else ''
                result = request(info, f'/human/jobs/{selection}/logs' + suffix, human=True, timeout=3)
                logs = result.get('tail') or result.get('error') or 'No log text.'
            except Exception as exc:
                logs = 'Cannot read log: ' + str(exc)
            logs_for = identity
        screen.erase()
        height, width = screen.getmaxyx()
        line(0, "GPU MCP Monitor  " + str(snapshot.get("repo_root", "connecting")), curses.A_BOLD)
        line(1, '  '.join(f'[{page.title()}]' if page == mode else page.title() for page in pages), curses.A_BOLD)
        line(2, 'Search: ' + (draft[:cursor] + '▏' + draft[cursor:] if searching else query or '/ to search saved jobs'))
        health = snapshot.get("health", {})
        reservation_label = 'Saved GPU reservations' if snapshot.get('saved_only') else 'Active GPU reservations'
        line(3, f"{reservation_label}: {len(active_reservations)} | Jobs: {len(jobs)}/{len(all_jobs)}"
             + (' | GPU reservations only' if reservations_only else ''))
        help_text = ('Enter apply  Esc cancel  Ctrl+U clear' if searching else
                     '←→ pages  ↑↓ select/scroll  / search  v GPU  r refresh  Esc close'
                     + ('  c parent/case' if cases else '')
                     + ('  b repos' if chooser else ''))
        line(height - 2, help_text)
        issue = error or snapshot.get('notice') or next((f'{name}: {value}' for name, value in health.items()
                              if value not in ('ready', 'starting', None)), '')
        if issue:
            line(height - 3, issue)
        if mode == 'jobs':
            line(4, 'TASK / RESOURCE / STATUS (saved)', curses.A_BOLD)
            cards = []
            window = max(1, (height - 8) // 3)
            nearby = {r['job_id'] for r in jobs[max(0, index - window + 1):index + window]}
            read_progress = False
            for item in jobs:
                identity = (item['job_id'], item.get('active_attempt_id'))
                cached = progress_cache.get(identity)
                lifecycle = item.get('job_lifecycle')
                changed_state = cached is not None and cached.get('lifecycle') != lifecycle
                if (not searching and not error and not read_progress and item['job_id'] in nearby
                        and (lifecycle in ('running', 'stopping') or item['job_id'] == selection and (cached is None or changed_state))
                        and item.get('output_file')
                        and (cached is None or changed_state or time.monotonic() >= cached['next_read'])):
                    # Human-only reads, bounded to the log tail; no agent wake or job save.
                    try:
                        tail = request(info, f"/human/jobs/{item['job_id']}/logs", human=True, timeout=1)
                        observed_progress = parse_log_progress(tail.get('tail') or '')
                    except Exception:
                        observed_progress = None
                    next_read = time.monotonic() + 5 if lifecycle in ('running', 'stopping') else float('inf')
                    cached = {'progress': observed_progress, 'next_read': next_read, 'lifecycle': lifecycle}
                    progress_cache[identity] = cached
                    read_progress = True
                displayed = dict(item)
                if cached and cached['progress']:
                    displayed['progress'] = cached['progress']
                row = task_row(displayed)
                tag = f"[{row['role']}] " if row['role'] else ''
                status = ('running' if item.get('job_lifecycle') == 'running' and not snapshot.get('saved_only')
                          else row['status'])
                timing = f" / {row['elapsed']}" if row['elapsed'] != '—' else ''
                title_width = max(8, width - len(tag) - len(row['resource']) - len(status) - len(timing) - 10)
                card = [f"{tag}{row['title'][:title_width]}  |  {row['resource']}  |  {status}{timing}"]
                script = Path(row['script'])
                try:
                    script = script.relative_to(snapshot.get('repo_root', ''))
                except ValueError:
                    pass
                card.append(f"  Code: {script} | Session: {row['session']} | Job: {row['job'][-8:]}")
                if row['args']:
                    arguments = textwrap.wrap('  Args: ' + row['args'], max(8, width - 1),
                                              subsequent_indent='        ', break_on_hyphens=False)
                    card.extend(arguments[:3])
                    if len(arguments) > 3:
                        card[-1] = card[-1][:max(0, width - 15)] + ' … (Details)'
                if item.get('gpu_index') is not None:
                    card.append(f"  Reservation: {row['reservation']}")
                if item.get('job_kind') == 'mps':
                    counts = item.get('case_counts', {})
                    card.append('  Runs: ' + ', '.join(f'{v} {k}' for k, v in counts.items() if v)
                                + f" | Limit: {item.get('max_concurrent')}")
                if displayed.get('progress'):
                    value = progress_text(displayed['progress'], width=max(12, min(40, width - 12)))
                    if value != '—':
                        message = safe_text(displayed['progress'].get('message'))
                        card.append('  Progress: ' + value + ('  ' + message if message and message != value else ''))
                lease = snapshot.get('monitoring', {}).get(item['job_id'], {}).get('lease', {})
                if lease.get('error'):
                    card.append('  Reservation updates failed; see Details.')
                elif (snapshot.get('server_instance_id') and item.get('server_instance_id')
                      and item['server_instance_id'] != snapshot['server_instance_id']
                      and (item.get('active_reservation') or lifecycle in ('running', 'stopping', 'launching'))):
                    card.append('  Owning server stopped; see Details.')
                cards.append((item, card))
            available = max(1, height - 8)
            start = index
            used = len(cards[index][1]) + 1 if cards else 0
            while start > 0 and used + len(cards[start - 1][1]) + 1 <= available:
                start -= 1
                used += len(cards[start][1]) + 1
            y = 5
            for record, card in cards[start:]:
                if y > 5 and y + len(card) > height - 3:
                    break
                style = colors.get(STATUS_COLORS.get(record.get('job_lifecycle'), 'yellow'), 0)
                if record["job_id"] == selection:
                    style |= curses.A_REVERSE
                for n, value in enumerate(card):
                    if y + n < height - 3:
                        line(y + n, value, style if n == 0 else 0)
                y += len(card) + 1
            if not jobs:
                line(5, 'No matching jobs.' if query or searching else 'No active GPU reservations.' if reservations_only else 'No saved jobs.')
        else:
            if mode == 'runs':
                text = '\n'.join(('› ' if c['case_id'] == case_selection else '  ')
                    + f"{c['case_id']}: {c.get('job_title') or Path(c['script_path']).name} | {c['state']}"
                    + '\n    Args: ' + ' '.join(c.get('args', [])) for c in cases) or 'Cases are starting.'
            elif mode == 'events':
                events = [e for e in snapshot.get('events', []) if selection is None or e['job_id'] == selection]
                text = '\n'.join(messages({'events': events})) or 'No events recorded.'
            elif not record:
                text = 'No job selected.'
            elif mode == 'logs':
                text = logs
            else:
                observed = snapshot.get('monitoring', {}).get(selection, {})
                lease = observed.get('lease', {})
                diagnostics = {key: value for key, value in {
                    'reservation_renewed_at': lease.get('last_success_at'),
                    'reservation_update_error': lease.get('error'),
                    'completion_file_checked_at': observed.get('last_result_scan_at'),
                    'completion_file_visible': observed.get('result_file_visible'),
                }.items() if value not in (None, '')}
                detail = dict(selected_case) if selected_case else {k: v for k, v in record.items() if k != 'mps_spec'}
                if diagnostics:
                    detail['monitoring'] = diagnostics
                cached = progress_cache.get(identity)
                if cached and cached['progress']:
                    detail['reported_progress'] = cached['progress']
                text = json.dumps(detail, indent=2, ensure_ascii=False)
            text_lines, selected_line = [], 0
            for value in safe_text(text).splitlines():
                if mode == 'runs' and value.startswith('› '):
                    selected_line = len(text_lines)  # Before wrapping even a 64-character case ID.
                text_lines.extend(textwrap.wrap(value, max(1, width - 1),
                    replace_whitespace=False, drop_whitespace=False, break_on_hyphens=False) or [''])
            page_size = max(1, height - 8)
            max_offset = max(0, len(text_lines) - page_size)
            if mode == 'runs' and case_selection:
                offsets[mode] = max(0, min(offsets[mode], selected_line))
                if selected_line >= offsets[mode] + page_size:
                    offsets[mode] = selected_line - page_size + 1
            offset = max_offset if offsets[mode] is None else min(offsets[mode], max_offset)
            line(4, (selected_case.get('job_title') or selected_case['case_id']) if selected_case else task_row(record)['title'] if record else 'Repository', curses.A_BOLD)
            line(5, (selection or 'Repository') + f' | Lines {offset + 1}–{min(offset + page_size, len(text_lines))}/{len(text_lines)}')
            for n, value in enumerate(text_lines[offset:offset + page_size]):
                line(6 + n, value)
        screen.refresh()
        try:
            raw_key = screen.get_wch()
        except curses.error:
            continue
        key = ord(raw_key) if isinstance(raw_key, str) else raw_key
        if searching:
            if key == 27:
                searching = False
                mode, selection = search_anchor
            elif key in (10, 13, curses.KEY_ENTER):
                query, searching = draft, False
            elif key == 21:
                draft, cursor = '', 0
            elif key in (8, 127, curses.KEY_BACKSPACE):
                if cursor:
                    draft = draft[:cursor - 1] + draft[cursor:]
                    cursor -= 1
            elif key == curses.KEY_DC:
                draft = draft[:cursor] + draft[cursor + 1:]
            elif key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(draft), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(draft)
            elif isinstance(raw_key, str) and raw_key.isprintable() and len(draft) < 256:
                draft = draft[:cursor] + raw_key + draft[cursor:]
                cursor += 1
            continue
        if key in (27, ord("q"), 3):
            return
        if key == ord('/'):
            draft, cursor, searching, search_anchor = query, len(query), True, (mode, selection)
            mode = 'jobs'
            continue
        if key == ord('b') and chooser:
            return 'chooser'
        if key == ord('v'):
            reservations_only = not reservations_only
            mode = 'jobs'
        if cases and key == ord('c'):
            case_selection = None if case_selection else case_ids[0]
            logs_for = None
            offsets.update(details=0, logs=None)
        if mode == 'runs' and case_ids and key in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k')):
            step = 1 if key in (curses.KEY_DOWN, ord('j')) else -1
            index_case = case_ids.index(case_selection) if case_selection in case_ids else (-1 if step == 1 else len(case_ids))
            case_selection = case_ids[max(0, min(len(case_ids) - 1, index_case + step))]
            logs_for = None
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            step = 1 if key == curses.KEY_RIGHT else -1
            mode = pages[(pages.index(mode) + step) % len(pages)]
        previous_selection = selection
        if ids and (key == ord(']') or mode == 'jobs' and key in (curses.KEY_DOWN, ord('j'))):
            selection = ids[min(index + 1, len(ids) - 1)]
        if ids and (key == ord('[') or mode == 'jobs' and key in (curses.KEY_UP, ord('k'))):
            selection = ids[max(index - 1, 0)]
        if selection != previous_selection:
            offsets = {'details': 0, 'logs': None, 'events': None, 'runs': 0}
        elif mode not in ('jobs', 'runs'):
            if key in (curses.KEY_DOWN, ord('j'), curses.KEY_NPAGE):
                offsets[mode] = min(max_offset, offset + (page_size if key == curses.KEY_NPAGE else 1))
            if key in (curses.KEY_UP, ord('k'), curses.KEY_PPAGE):
                offsets[mode] = max(0, offset - (page_size if key == curses.KEY_PPAGE else 1))
            if key == curses.KEY_HOME:
                offsets[mode] = 0
            if key == curses.KEY_END:
                offsets[mode] = None
        if key in (10, 13, curses.KEY_ENTER):
            mode = 'details' if mode in ('jobs', 'runs') else 'jobs'
        if key == ord("r"):
            refresh_at = 0
            logs_for = None
            progress_cache.clear()
        if key == ord('l'):
            mode = 'logs'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-base", type=Path)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("open the monitor directly in a human terminal; it has no agent-output mode")
    if args.config is None:
        from gpu_mcp_policy_hook import find_policy_file
        args.config = find_policy_file(Path.cwd())
    if args.config is None:
        parser.error("no gpu-mcp.toml found; supply --config")
    info = connect_viewer(args.config, base=args.state_base)
    try:
        curses.wrapper(view, info)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
