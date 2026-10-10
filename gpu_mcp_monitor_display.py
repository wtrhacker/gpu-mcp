"""Small display adapter, adapted from Scientific Workflow's MIT UI module.

Only progress formatting, timing, status colors and bounded messages are
ported. No execution runtime, cancellation or scientific interpretation.
See third_party/scientific-workflow/NOTICE.md and LICENSE.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shlex

MESSAGE_HISTORY = 100
STATUS_COLORS = {"launching": "gray", "running": "cyan", "stopping": "yellow",
                 "succeeded": "green", "finished": "green", "failed": "red",
                 "launch_outcome_unknown": "yellow", "process_gone_unknown_outcome": "yellow"}


def safe_text(value) -> str:
    # Logs are data, including terminal escapes. Never execute control bytes.
    return "".join(c if c == "\n" or c.isprintable() else "?" for c in str(value or ""))


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def progress_text(progress: dict | None, width: int = 24) -> str:
    if not progress:
        return "—"
    iteration, target = progress.get("current"), progress.get("total")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
        return safe_text(progress.get('message'))[:width] or '—'
    valid_target = isinstance(target, int) and not isinstance(target, bool) and target >= 0
    count = f"{iteration}/{target}" if valid_target else str(iteration)
    if len(count) > width:
        # Preserve the upstream rule: omit instead of clipping a count into a
        # different number when the terminal is narrow.
        return "." * min(width, 3)
    bar_width = min(max(0, width - len(count) - 1), 16)
    if valid_target and bar_width:
        ratio = 1 if target == 0 else min(1, iteration / target)
        filled = math.floor(ratio * bar_width + 0.5)
        return count + " " + "█" * filled + "░" * (bar_width - filled)
    return count


def parse_log_progress(text: str) -> dict | None:
    """Read opt-in work counts; log data never determines lifecycle or ownership."""
    prefix = 'GPU_MCP_PROGRESS '
    for line in reversed(text.splitlines()):
        if not line.startswith(prefix) or len(line) > 4096:
            continue
        try:
            value = json.loads(line[len(prefix):])
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        current, total = value.get('current'), value.get('total')
        if (isinstance(current, bool) or not isinstance(current, int) or current < 0
                or isinstance(total, bool) or not isinstance(total, int) or total <= 0):
            continue
        result = {'current': current, 'total': total}
        if isinstance(value.get('message'), str):
            result['message'] = safe_text(value['message'])[:120]
        return result
    return None


def matches_job(record: dict, query: str) -> bool:
    values = [record.get(key, '') for key in ('job_id', 'job_title', 'script_path', 'script_name',
              'host', 'origin_thread_id', 'job_lifecycle', 'job_role', 'job_kind')]
    values += list(record.get('args') or [])
    if record.get('gpu_index') is not None:
        values.append(f"GPU {record['gpu_index']}")
    haystack = ' '.join(str(value) for value in values).casefold()
    return all(word in haystack for word in query.casefold().split())


def elapsed(record: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    outcome = record.get("outcome") or {}
    # Submission time is not a script start or proof that the process is alive.
    start = outcome.get("started_at") or record.get('execution_started_at')
    end = outcome.get("ended_at")
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
        return format_duration((ended - started).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return "—"


def task_row(record: dict) -> dict:
    gpu = record.get("gpu_index")
    script = record.get('script_path') or record.get('script_name') or ''
    state = record.get("job_lifecycle", "unknown")
    if state == "finished" and not record.get("outcome"):
        state = "finished / result unknown"
    return {"job": record["job_id"], "session": (record.get("origin_thread_id") or "unknown")[-8:],
            'title': safe_text(record.get('job_title')) or Path(script).name or record['job_id'],
            'role': {'smoke': 'Smoke', 'main': 'Main', 'one_off': 'One-off'}.get(record.get('job_role'),
                      'Build' if record.get('job_kind') == 'build' else ''),
            "kind": record.get("job_kind", "gpu"), "script": script,
            'args': safe_text(shlex.join([str(arg) for arg in record.get('args') or []])),
            'reservation': ('none (CPU)' if gpu is None else 'unknown' if record.get('active_reservation') is None
                            else 'held' if record['active_reservation'] else 'released'),
            "resource": f"{record.get('host', '?')} / " + ("CPU" if gpu is None else f"GPU {gpu}"),
            "status": 'running (last known)' if state == 'running' else state, "elapsed": elapsed(record),
            "checked": record.get("last_status_checked_at") or "not checked",
            "progress": progress_text(record.get("progress"))}


def messages(snapshot: dict) -> list[str]:
    return [safe_text(f"{e['updated_at']}  {e['job_id']}  {e['kind']}  delivery={e['state']}  "
                      f"{e['payload'].get('message') or e['payload'].get('error_summary') or ''}")
            for e in snapshot.get("events", [])[-MESSAGE_HISTORY:]]
