"""Protected Codex job ownership and recoverable notification state.

The repository's job.json files remain display mirrors in shared mode. This
database is outside job write roots and commits a result and its owner event
together, including for CPU-only jobs.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import threading
import uuid

import gpu_mcp_reservations as reservations

STALE_AFTER_SEC = 600
TERMINAL_STATES = frozenset({"succeeded", "failed", "finished"})
UNCERTAIN_STATES = frozenset({"process_gone_unknown_outcome", "launch_outcome_unknown"})
DELIVERABLE_STATES = frozenset({"pending", "sending", "uncertain", "archived"})


class SessionAccessError(PermissionError):
    pass


class StaleAttemptError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Origin:
    thread_id: str
    session_id: str | None = None
    window_id: str | None = None

    @classmethod
    def from_meta(cls, meta) -> "Origin":
        if hasattr(meta, "model_dump"):
            meta = meta.model_dump(exclude_none=True)
        if not isinstance(meta, dict):
            raise SessionAccessError("Codex caller identity is missing")
        thread = meta.get("threadId")
        try:
            thread = str(uuid.UUID(thread))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SessionAccessError("Codex caller identity is missing or invalid") from exc
        inner = meta.get("x-codex-turn-metadata")
        if isinstance(inner, dict) and inner.get("thread_id", thread) != thread:
            raise SessionAccessError("Codex caller identity is inconsistent")
        return cls(thread, meta.get("sessionId"), meta.get("windowId"))


def runtime_root(config: str | Path, *, base: str | Path | None = None) -> Path:
    path = Path(config).expanduser().resolve()
    # Valid policies require repo_root == config.parent; bootstrap uses that same
    # identity before the policy exists. Subdirectories and alternate file names
    # must not create another job owner for the same repository.
    key = hashlib.sha256(f"{socket.gethostname()}\0{path.parent}".encode()).hexdigest()[:24]
    home = Path(base) if base is not None else Path.home() / "gpu-mcp/state/codex"
    return home / key


def private_directory(path: Path) -> None:
    reservations._assert_no_symlink_ancestors(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    reservations._assert_no_symlink_ancestors(path)
    if path.stat().st_uid != os.getuid():
        raise PermissionError("GPU MCP control directory belongs to another user")
    path.chmod(0o700)


def event_payload(record: dict) -> dict:
    outcome = record.get("outcome") or {}
    return {"job_id": record["job_id"], "attempt_id": record.get("active_attempt_id"),
            "job_kind": record.get("job_kind", "gpu"),
            "job_lifecycle": record.get("job_lifecycle"),
            "event_kind": record.get("job_lifecycle"),
            "terminal_result_known": bool(record.get("outcome")),
            "output_file": record.get("output_file"),
            "exit_code": outcome.get("exit_code"), "signal": outcome.get("signal"),
            "error_summary": str(outcome.get("error_summary") or record.get("reason") or "")[:1000]}


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        private_directory(self.root)
        self.path = self.root / "jobs.sqlite3"
        if self.path.is_symlink():
            raise PermissionError("refusing a symlinked job database")
        self._mutex = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._local = threading.local()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, origin TEXT NOT NULL,
                    record TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, origin TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL, error TEXT,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id));
                CREATE INDEX IF NOT EXISTS pending_events ON events(state);
            """)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self, job_id: str):
        if not reservations.is_job_id(job_id):
            raise ValueError("invalid job ID")
        with self._mutex:
            mutex = self._locks.setdefault(job_id, threading.RLock())
        with mutex:
            depths = getattr(self._local, "depths", {})
            self._local.depths = depths
            depth = depths.get(job_id, 0)
            depths[job_id] = depth + 1
            fd = None
            try:
                if not depth:
                    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(self.root / f"{job_id}.lock", flags, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                depths[job_id] -= 1
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def get(self, job_id: str, origin: str | None = None) -> dict | None:
        if not reservations.is_job_id(job_id):
            return None
        with self._connect() as db:
            row = db.execute("SELECT origin,record FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or (origin is not None and row["origin"] != origin):
            return None
        return json.loads(row["record"])

    def records(self, origin: str | None = None) -> list[dict]:
        with self._connect() as db:
            rows = (db.execute("SELECT record FROM jobs ORDER BY updated_at DESC") if origin is None
                    else db.execute("SELECT record FROM jobs WHERE origin=? ORDER BY updated_at DESC", (origin,)))
            return [json.loads(row[0]) for row in rows]

    def save(self, record: dict, *, origin: Origin | None = None,
             expected_attempt: str | None = None, register: bool = False) -> dict:
        record = dict(record)
        job_id = record["job_id"]
        attempt = record.get("active_attempt_id")
        if not reservations.is_attempt_id(attempt):
            raise ValueError("invalid attempt ID")
        with self.lock(job_id), self._connect() as db:
            row = db.execute("SELECT origin,record FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                if not register or origin is None:
                    raise SessionAccessError("job unavailable to this session")
                owner = origin.thread_id
                record["origin_session_id"] = origin.session_id
                record["origin_window_id"] = origin.window_id
            else:
                owner = row["origin"]
                previous = json.loads(row["record"])
                if origin is not None and owner != origin.thread_id:
                    raise SessionAccessError("job unavailable to this session")
                if expected_attempt is not None and previous.get("active_attempt_id") != expected_attempt:
                    raise StaleAttemptError("the active attempt changed")
                if attempt != previous.get("active_attempt_id") and expected_attempt is None:
                    raise StaleAttemptError("an attempt change requires a conditional update")
                record["origin_session_id"] = previous.get("origin_session_id")
                record["origin_window_id"] = previous.get("origin_window_id")
            record["origin_thread_id"] = owner
            now = reservations.iso_timestamp()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                       "record=excluded.record,updated_at=excluded.updated_at",
                       (job_id, owner, json.dumps(record), now))
            state = record.get("job_lifecycle")
            if state != 'running':
                db.execute("UPDATE events SET state='superseded',error='job state changed' "
                           "WHERE job_id=? AND attempt_id=? AND kind='poll' AND state IN ('pending','archived')",
                           (job_id, attempt))
            if state not in UNCERTAIN_STATES:
                db.execute("UPDATE events SET state='resolved',error='resolved by a newer observation' "
                           "WHERE job_id=? AND attempt_id=? AND kind IN (?,?) AND state!='accepted'",
                           (job_id, attempt, *sorted(UNCERTAIN_STATES)))
            if state in TERMINAL_STATES and not record.get("active_reservation"):
                db.execute("UPDATE events SET state='resolved',error='job management completed' "
                           "WHERE job_id=? AND attempt_id=? AND kind IN ('monitor_failure','lease_failure') "
                           "AND state IN ('pending','archived')", (job_id, attempt))
            if state in TERMINAL_STATES | UNCERTAIN_STATES:
                payload = event_payload(record)
                episode = record.get('verification_pending_since', '') if state in UNCERTAIN_STATES else ''
                event_id = hashlib.sha256(f"{job_id}\0{attempt}\0{state}".encode()
                                         + (f'\0{episode}'.encode() if episode else b'')).hexdigest()
                db.execute("INSERT OR IGNORE INTO events "
                           "(event_id,job_id,attempt_id,origin,kind,payload,updated_at) VALUES (?,?,?,?,?,?,?)",
                           (event_id, job_id, attempt, owner, state, json.dumps(payload), now))
            return record

    def events(self, *, pending: bool = False, origin: str | None = None) -> list[dict]:
        conditions, values = [], []
        if pending:
            conditions.append("state IN ('pending','sending','uncertain','archived')")
        if origin is not None:
            conditions.append("origin = ?")
            values.append(origin)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as db:
            order = "ASC" if pending else "DESC"
            rows = db.execute("SELECT * FROM events" + where + f" ORDER BY updated_at {order} LIMIT 500", values)
            result = [dict(row, payload=json.loads(row["payload"])) for row in rows]
            return result if pending else list(reversed(result))

    def event(self, event_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            return dict(row, payload=json.loads(row["payload"])) if row is not None else None

    def delivery(self, event_id: str, state: str, *, error: str | None = None):
        if state not in {"pending", "sending", "uncertain", "accepted", "archived"}:
            raise ValueError("invalid delivery state")
        with self._connect() as db:
            db.execute("UPDATE events SET state=?,error=?,updated_at=?,attempts=attempts+? WHERE event_id=?",
                       (state, error, reservations.iso_timestamp(), int(state == "sending"), event_id))

    def notice(self, record: dict, kind: str, message: str, *, key: str):
        """Save an owner notice without manufacturing a lifecycle transition."""
        with self.lock(record["job_id"]), self._connect() as db:
            row = db.execute("SELECT origin,record FROM jobs WHERE job_id=?", (record["job_id"],)).fetchone()
            if row is None:
                raise SessionAccessError("job unavailable to this session")
            current = json.loads(row["record"])
            if current["active_attempt_id"] != record["active_attempt_id"]:
                raise StaleAttemptError("the active attempt changed")
            if kind == 'poll':
                if (current.get('job_lifecycle') != 'running' or not current.get('last_status_checked_at')
                        or current['last_status_checked_at'] != record.get('last_status_checked_at')):
                    return None
                payload = {'job_id': current['job_id'], 'attempt_id': current['active_attempt_id'],
                           'event_kind': 'poll', 'job_lifecycle': 'running',
                           'checked_at': current['last_status_checked_at'],
                           'next_backend_check_at': current.get('next_poll_after'), 'message': message[:200]}
                if current.get('job_title'):
                    payload['job_title'] = current['job_title']
            else:
                payload = dict(event_payload(current), event_kind=kind, message=message[:1000])
            event_id = hashlib.sha256(f"{record['job_id']}\0{record['active_attempt_id']}\0{kind}\0{key}".encode()).hexdigest()
            if kind != "milestone":
                db.execute("UPDATE events SET state='superseded',error='newer notice for the same condition' "
                           "WHERE job_id=? AND attempt_id=? AND kind=? AND event_id!=? "
                           "AND state IN ('pending','archived')",
                           (record["job_id"], record["active_attempt_id"], kind, event_id))
            db.execute("INSERT OR IGNORE INTO events "
                       "(event_id,job_id,attempt_id,origin,kind,payload,updated_at) VALUES (?,?,?,?,?,?,?)",
                       (event_id, record["job_id"], record["active_attempt_id"], row["origin"], kind,
                        json.dumps(payload), reservations.iso_timestamp()))
            return event_id

    def retry_notification(self, event_id: str, origin: str, reason: str):
        if not reason.strip():
            raise ValueError("retrying a notification needs a reason; acknowledgement loss may cause a duplicate")
        with self._connect() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=? AND origin=?", (event_id, origin)).fetchone()
            if row is None:
                raise SessionAccessError("notification unavailable to this session")
            if row["state"] not in {"uncertain", "sending"}:
                raise ValueError("only uncertain notification delivery can be explicitly retried")
            db.execute("UPDATE events SET state='pending',error=?,updated_at=? WHERE event_id=?",
                       ("explicit owner retry: " + reason[:500], reservations.iso_timestamp(), event_id))

    def clear_old_events(self, job_id: str, current_attempt: str):
        # Historical results remain inspectable, but cannot wake a newer attempt.
        with self._connect() as db:
            db.execute("UPDATE events SET state='superseded',error='superseded by a newer attempt' "
                       "WHERE job_id=? AND attempt_id!=? AND state!='accepted'", (job_id, current_attempt))
