"""Session-scoped shared job management and quiet background reconciliation."""
from __future__ import annotations

import contextlib
import contextvars
import inspect
import json
from pathlib import Path
import threading
import time

from gpu_mcp_codex_daemon import DaemonClient, DeliveryUncertain
from gpu_mcp_codex_state import DELIVERABLE_STATES, JobStore, Origin, SessionAccessError, TERMINAL_STATES, UNCERTAIN_STATES
import gpu_mcp_reservations as reservations

VERIFICATION_GRACE_SEC = 10
VERIFICATION_RETRY_SEC = 2
VERIFICATION_LATE_RETRY_SEC = 30

MONITORING_GUIDANCE = (
    "With the Codex shared launcher, backend code checks jobs at the interval you choose. "
    "next_poll_after is the next backend check, not an instruction to start a timer. "
    "Short polling results are delivered privately when you are active; idle updates are combined. "
    "Completion and persistent monitoring problems reach this conversation automatically and wake it when idle. "
    "Return control when only waiting remains: the job stays monitored and its result resumes this conversation. "
    "Waiting for a final result alone needs no sleep-and-check loop or repeated status/list calls. "
    "Status, logs and intermediate outputs remain available for a concrete question or decision. "
    "Routine healthy updates require no human-facing commentary. "
    "Report job failures and monitoring problems that need attention. "
    "A result notification requires no acknowledgement tool call or immediate follow-up work. "
    "Backend code handles booking cleanup; finish is explicit cleanup for an unresolved job, not a success acknowledgement."
)


class CodexRuntime:
    def __init__(self, server, root: Path, *, daemon=None):
        self.server = server
        self.store = JobStore(root)
        self.daemon = daemon or DaemonClient()
        self._origin = contextvars.ContextVar("gpu_mcp_origin", default=None)
        self._operation = contextvars.ContextVar("gpu_mcp_operation", default=None)
        self._stop = threading.Event()
        self._threads = []
        self._outcome_signatures = {}
        self._monitor_observations = {}
        self._observation_lock = threading.Lock()
        self._release_after = {}
        self._delivery_after = {}
        self._verification_retry_after = {}
        self._admission = threading.Lock()
        self._calls = 0
        self._stopping = False
        self.request_shutdown = None
        self.health = {"monitor": "ready", "delivery": "ready"}
        self.validate_policy(server.CONFIG_POLICY)
        if server.CONFIG_POLICY is not None:
            try:
                reservations.ensure_repo_job_state_root(server.REPO_ROOT)
            except (OSError, ValueError) as exc:
                # Historical job diagnostics remain available. Launch performs
                # this check again before booking or contacting an executor.
                self.health['launch_state'] = str(exc)
        for name in ('run_python_on_gpu', 'build_python_artifact', 'manage_gpu_job'):
            tool = server.mcp._tool_manager.get_tool(name)
            if tool is not None and not tool.description.startswith(MONITORING_GUIDANCE):
                tool.description = MONITORING_GUIDANCE + '\n\n' + tool.description

    def validate_policy(self, policy):
        if policy is None:
            return
        if policy.repo_root != self.server.POLICY_CONFIG_PATH.parent:
            raise ValueError("the shared backend cannot change its repository identity")
        for root in (*policy.write_roots, *policy.output_roots):
            try:
                self.store.root.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            raise ValueError("Codex job authority overlaps approved job write roots")

    def origin(self) -> Origin:
        origin = self._origin.get()
        if origin is None:
            raise SessionAccessError("Codex caller identity is missing")
        return origin

    @contextlib.contextmanager
    def acting_as(self, origin: Origin):
        token = self._origin.set(origin)
        try:
            yield
        finally:
            self._origin.reset(token)

    def read(self, job_id):
        return self.store.get(job_id, self.origin().thread_id)

    def records(self):
        return self.store.records(self.origin().thread_id)

    def can_view(self, job_id):
        return isinstance(job_id, str) and self.read(job_id) is not None

    def register(self, record):
        if self.store.get(record["job_id"]) is not None:
            return self.persist(record)
        record = dict(record, repo_root=str(self.server.REPO_ROOT))
        record.setdefault("job_lifecycle", "launching")
        if record.get("job_kind", "gpu") == "gpu" and reservations.is_reservation_key(record.get("reservation_key")):
            # GPU registration follows successful acquisition. Legacy launch
            # records omitted this field; shared monitoring needs it explicitly.
            record.setdefault("active_reservation", True)
        return self.store.save(record, origin=self.origin(), register=True)

    def persist(self, record):
        record = dict(record)
        if record.get('job_lifecycle') in UNCERTAIN_STATES:
            previous = self.store.get(record['job_id']) or {}
            since = (previous.get('verification_pending_since')
                     if previous.get('active_attempt_id') == record.get('active_attempt_id')
                     and previous.get('job_lifecycle') in UNCERTAIN_STATES else None)
            record['verification_pending_since'] = since or self.server.datetime.now(self.server.timezone.utc).isoformat().replace('+00:00', 'Z')
        else:
            record.pop('verification_pending_since', None)
        operation = self._operation.get()
        expected = operation.get("attempt") if operation and operation["job_id"] == record["job_id"] else None
        saved = self.store.save(record, origin=self.origin(), expected_attempt=expected)
        if operation and operation["job_id"] == record["job_id"]:
            operation["attempt"] = saved["active_attempt_id"]
        self.store.clear_old_events(saved["job_id"], saved["active_attempt_id"])
        return saved

    @contextlib.contextmanager
    def operation(self, record):
        with self.store.lock(record["job_id"]):
            latest = self.read(record["job_id"])
            if latest is None:
                raise SessionAccessError("job unavailable to this session")
            token = self._operation.set({"job_id": latest["job_id"], "attempt": latest["active_attempt_id"]})
            try:
                yield latest
            finally:
                self._operation.reset(token)

    def target(self, arguments):
        job_id = arguments.get("job_id")
        key = arguments.get("reservation_key")
        if job_id is not None:
            record = self.read(job_id)
            if record is None or (key is not None and key != record.get("reservation_key")):
                raise SessionAccessError("job unavailable to this session")
            return record
        records = self.records()
        records = [r for r in records if (key is not None and key == r.get("reservation_key")) or
                   (key is None and r.get("job_lifecycle") not in TERMINAL_STATES)]
        if len(records) != 1:
            raise SessionAccessError("choose an explicit job_id from this session's jobs")
        return records[0]

    def invoke(self, function, args, kwargs):
        with self._admission:
            if self._stopping:
                return json.dumps({"status": "refused", "reason": "the shared backend is stopping; reconnect"})
            self._calls += 1
        try:
            return self._invoke(function, args, kwargs)
        finally:
            with self._admission:
                self._calls -= 1

    def shutdown_if_idle(self):
        with self._admission:
            active = [r for r in self.store.records() if r.get("server_instance_id") == self.server.SERVER_INSTANCE_ID
                      and (r.get("job_lifecycle") not in TERMINAL_STATES | {"process_gone_unknown_outcome"}
                           or r.get("active_reservation", bool(r.get('reservation_key'))))]
            if self._calls or active:
                return {"status": "refused", "reason": "the backend has active jobs or requests"}
            self._stopping = True
            if self.request_shutdown:
                self.request_shutdown()
            return {"status": "stopping"}

    def _invoke(self, function, args, kwargs):
        try:
            meta = self.server.mcp.get_context().request_context.meta
            origin = Origin.from_meta(meta)
            bound = inspect.signature(function).bind(*args, **kwargs)
            bound.apply_defaults()
            arguments = bound.arguments
            with self.acting_as(origin):
                name = function.__name__
                if name == "manage_gpu_job":
                    record = self.target(arguments)
                    arguments["job_id"] = record["job_id"]
                    with self.operation(record):
                        result = function(**arguments)
                elif name == "check_gpu_processes":
                    return json.dumps({"status": "refused", "reason":
                        "Inspect this session's jobs with manage_gpu_job(detail='full')."})
                elif name == "kill_gpu_process":
                    pid, host = arguments.get("pid"), arguments.get("host")
                    own = [r for r in self.records() if r.get("remote_pid") == pid and
                           r.get("active_reservation") and r.get("server_instance_id") == self.server.SERVER_INSTANCE_ID and
                           isinstance(host, str) and r.get("host") == self.server._canonical_policy_host(host)]
                    if not own:
                        raise SessionAccessError("process unavailable to this session")
                    with self.operation(own[0]) as latest:
                        metadata, _ = self.server._owned_lifecycle_metadata(latest)
                        if (metadata is None or metadata.get("remote_pid") != pid or
                                self.server._inspect_reservation_process(metadata)["status"] != "alive"):
                            raise SessionAccessError("process unavailable to this session")
                        result = function(**arguments)
                else:
                    smoke = arguments.get("smoke_job_id")
                    if smoke is not None and not self.can_view(smoke):
                        raise SessionAccessError("job unavailable to this session")
                    result = function(**arguments)
                detail = arguments.get("detail", "summary")
                if name == "manage_gpu_job" and arguments.get("action") in {"logs", "notifications"}:
                    detail = "full"
                return self.present(name, result, detail)
        except (SessionAccessError, LookupError, ValueError) as exc:
            return json.dumps({"status": "refused", "reason": str(exc)})

    def present(self, tool, result, detail):
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except (ValueError, TypeError):
            return result
        if not isinstance(payload, dict):
            return result
        if tool == "manage_gpu_job" and detail == "full" and payload.get("job_id"):
            record = self.read(payload["job_id"])
            if record is not None:
                payload["saved_job"] = record
                payload.setdefault("process", record.get("last_process_observation") or {
                    "remote_pid": record.get("remote_pid"), "process_fingerprint": record.get("process_fingerprint")})
        if tool == "list_gpu_reservations":
            payload["reservations"] = [r for r in payload.get("reservations", []) if self.can_view(r.get("job_id"))]
        elif tool == "check_gpus":
            # Physical availability is shared; foreign job diagnostics are not.
            allowed = {"host", "gpu_index", "name", "availability", "computed_state", "route",
                       "memory_total_mib", "memory_used_mib", "memory_free_mib", "utilization_percent",
                       "reservation_state", "gpu_memory_mib", "gpu_utilization_percent",
                       "index", "utilization_pct", "utilization_avg_pct", "memory_total_MiB", "memory_used_MiB", "memory_free_MiB"}
            payload = {"status": payload.get("status"), "gpus": [
                {k: v for k, v in row.items() if k in allowed} for row in payload.get("gpus", [])]}
        if detail == "full" or tool in {"preview_policy_reload", "reload_policy", "reject_policy_reload",
                                        "list_gpu_reservations", "check_gpus", "cluster_info", "list_gpu_jobs", "kill_gpu_process"}:
            return json.dumps(payload)
        fields = {"status", "action", "reason", "job_id", "attempt_id", "job_kind", "job_lifecycle",
                  "host", "gpu_index", "output", "output_file", "artifact", "next_poll_after",
                  "artifact_path", "smoke_evidence", "allowed_actions", "polling_state", "watch",
                  "last_status_checked_at", "remote_inspection_performed"}
        compact = {k: v for k, v in payload.items() if k in fields and v not in (None, "")}
        if 'next_poll_after' in compact:
            compact['next_backend_check_at'] = compact.pop('next_poll_after')
        if payload.get("job_lifecycle") == "running" and payload.get("status") not in {"refused", "error"}:
            compact["message"] = ("Job launched. Backend checks and result delivery are automatic."
                                  if payload.get("status") in {"launched", "retried", "launched_with_warning"} else
                                  "No new job check; monitoring is automatic."
                                  if payload.get("polling_state") in {"not_due", "not_due_yet"} or payload.get("status") == "not_due"
                                  else "Job process running at the last check. Monitoring is automatic.")
        if payload.get("job_lifecycle") == "failed":
            outcome = payload.get("outcome") or {}
            compact["exit_code"] = outcome.get("exit_code")
            compact["error_summary"] = str(outcome.get("error_summary") or "")[:500]
        if payload.get('job_lifecycle') in {'succeeded', 'failed'}:
            compact['management_complete'] = payload.get('active_reservation') is False
        return json.dumps(compact)

    def _recover_previous_job(self, record):
        if record.get("job_kind", "gpu") != "gpu":
            return False
        with self.acting_as(Origin(record["origin_thread_id"])), self.operation(record) as latest:
            if (latest["active_attempt_id"] != record["active_attempt_id"]
                    or latest["server_instance_id"] != record["server_instance_id"]):
                return True
            outcome = self.server._read_outcome_record(latest) or latest.get("outcome")
            lifecycle = self.server._job_lifecycle_from_outcome(outcome)
            if lifecycle is None:
                return False
            if (latest.get("job_lifecycle") != lifecycle or latest.get("outcome") != outcome
                    or latest.get("next_poll_after") is not None):
                latest = dict(latest, job_lifecycle=lifecycle, outcome=outcome, next_poll_after=None)
                self.server._write_repo_job_record(latest)
            if not latest.get("active_reservation", bool(latest.get("reservation_key"))):
                return True
            metadata = self.server._read_reservation_metadata(latest.get("reservation_key"))
            if (metadata is not None and self.server._job_record_matches_current_metadata(latest, metadata)
                    and not self.server._metadata_stale(metadata)):
                return True
            identity = (latest["job_id"], latest["active_attempt_id"])
            if self._release_after.get(identity, 0) > time.monotonic():
                return True
            response = json.loads(self.server._manage_gpu_job_impl(action="status", job_id=latest["job_id"]))
            if response.get("status") in {"error", "refused"}:
                raise RuntimeError(response.get("reason", "completed job recovery failed"))
            self._release_after[identity] = time.monotonic() + 30
            return True

    def monitor_once(self):
        if self.server._stale_policy_refusal():
            self.health["monitor"] = "policy requires activation or reload"
            for record in self.store.records():
                if record.get("job_lifecycle") not in TERMINAL_STATES:
                    self.alert(record, "monitor_failure", "Monitoring paused: review and activate the repository policy.")
            return
        now = self.server.datetime.now(self.server.timezone.utc)
        for record in self.store.records():
            state = record.get("job_lifecycle", "running")
            # Recover verified completions without adopting an older backend's
            # live jobs, heartbeats, stop/retry authority or reservation owner.
            if record.get("server_instance_id") != self.server.SERVER_INSTANCE_ID:
                if state in TERMINAL_STATES and not record.get("active_reservation", bool(record.get("reservation_key"))):
                    continue
                try:
                    recovered = self._recover_previous_job(record)
                    if not recovered and state not in TERMINAL_STATES:
                        self.store.notice(record, "backend_restarted",
                            "The owning backend stopped. Its reservation has not been adopted. Inspect this job before recovery.",
                            key=self.server.SERVER_INSTANCE_ID)
                except Exception as exc:
                    self.alert(record, "monitor_failure", "Completed job recovery failed: " + str(exc))
                continue
            if state == "launching" and self._calls:
                continue
            key = record.get('reservation_key')
            active = record.get("active_reservation", bool(key))
            if active and (key not in self.server.HEARTBEAT_MANAGER.owned_keys()
                                                     or not self.server.HEARTBEAT_MANAGER.is_healthy(key)):
                self.alert(record, "lease_failure", "Reservation check-ins failed: " + self.server.HEARTBEAT_MANAGER.health_reason(key))
            terminal = state in TERMINAL_STATES or state == "process_gone_unknown_outcome"
            if terminal and not active and state != "process_gone_unknown_outcome":
                continue
            if not terminal:
                for watch in record.get("watches", []):
                    if watch["attempt_id"] == record["active_attempt_id"] and self.server._parse_next_poll_after(watch["at"]) <= now:
                        self.store.notice(record, "milestone", watch["message"], key=watch["key"])
            path = (record.get("ready_file") or record.get("outcome_file") or
                    str(reservations.outcome_record_path(self.server.REPO_ROOT, record["job_id"], record["active_attempt_id"])))
            try:
                stat = Path(path).lstat()
                signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                scan_error = ''
            except OSError:
                signature = None
                scan_error = 'no result file visible'
            signature_key = (record["job_id"], record["active_attempt_id"])
            with self._observation_lock:
                self._monitor_observations[signature_key] = {
                    'last_result_scan_at': reservations.iso_timestamp(),
                    'result_file_visible': signature is not None,
                    'scan_note': scan_error,
                }
            changed_outcome = signature is not None and self._outcome_signatures.get(signature_key) != signature
            if (record.get("job_kind", "gpu") == "gpu" and not record.get("outcome")
                    and (signature is not None or state == "process_gone_unknown_outcome")):
                # Seeing a directory entry does not guarantee its contents were
                # readable on the prior pass. A newly readable valid result is
                # readiness evidence even if inode/mtime/size did not change.
                changed_outcome = changed_outcome or self.server._read_outcome_record(record) is not None
            uncertain = state in UNCERTAIN_STATES
            if uncertain and not changed_outcome:
                if self._verification_retry_after.get(signature_key, 0) > time.monotonic():
                    continue
                if (state == 'process_gone_unknown_outcome' and self.verification_age(record) >= VERIFICATION_GRACE_SEC
                        and record.get('launch_mode') != 'ssh'):
                    continue  # Local result reads continue; an already-gone process need not be checked repeatedly.
            if state in TERMINAL_STATES and self._release_after.get(signature_key, 0) > time.monotonic() and not changed_outcome:
                continue
            due = self.server._parse_next_poll_after(record.get("next_poll_after"))
            if state != "launching" and not terminal and not uncertain and not changed_outcome and (due is None or due > now):
                continue
            try:
                with self.acting_as(Origin(record["origin_thread_id"])), self.operation(record) as latest:
                    if latest["active_attempt_id"] != record["active_attempt_id"]:
                        continue
                    if state == "launching" and latest.get("active_reservation"):
                        metadata, _ = self.server._read_current_job_metadata(latest)
                        if metadata and metadata.get("server_instance_id") == self.server.SERVER_INSTANCE_ID:
                            latest = dict(latest, **{key: metadata.get(key) for key in
                                ("remote_pid", "remote_start_time", "remote_boot_id", "process_fingerprint")})
                            self.persist(latest)
                    arguments = {"action": "status", "job_id": latest["job_id"]}
                    if state == "launching":
                        arguments["early_poll_reason"] = "recover an incomplete launch acknowledgement"
                    elif uncertain:
                        arguments['early_poll_reason'] = 'Retry temporary job verification failure.'
                    response = json.loads(self.server._manage_gpu_job_impl(**arguments))
                    if response.get("status") in {"error", "refused"}:
                        raise RuntimeError(response.get("reason", "job inspection failed"))
                    self._outcome_signatures[signature_key] = signature
                    self._release_after[signature_key] = time.monotonic() + 30
                    checked = self.store.get(latest['job_id'])
                    if checked.get('job_lifecycle') in UNCERTAIN_STATES:
                        delay = VERIFICATION_RETRY_SEC if self.verification_age(checked) < VERIFICATION_GRACE_SEC else VERIFICATION_LATE_RETRY_SEC
                        self._verification_retry_after[signature_key] = time.monotonic() + delay
                    else:
                        self._verification_retry_after.pop(signature_key, None)
                        if (checked.get('job_lifecycle') == 'running' and checked.get('last_status_checked_at')
                                and checked['last_status_checked_at'] != record.get('last_status_checked_at')):
                            self.store.notice(checked, 'poll', 'Process observed running.',
                                              key=checked['last_status_checked_at'])
            except Exception as exc:
                self.alert(record, "monitor_failure", "Job check failed: " + str(exc))
        self.health["monitor"] = "ready"

    def verification_age(self, record, event=None):
        since = record.get('verification_pending_since') or (event or {}).get('updated_at')
        parsed = self.server._parse_iso_timestamp(since)
        return max(0, (self.server.datetime.now(self.server.timezone.utc) - parsed).total_seconds()) if parsed else VERIFICATION_GRACE_SEC

    def alert(self, record, kind, message):
        # A repeated infrastructure error wakes its owner at most once per ten
        # minutes, and never broadcasts to other conversations.
        self.store.notice(record, kind, message, key=str(int(time.time() // 600)))

    def deliver_once(self):
        # Result/problem delivery takes precedence over routine polling updates.
        for event in sorted(self.store.events(pending=True), key=lambda e: e['kind'] == 'poll'):
            if self._delivery_after.get(event["event_id"], 0) > time.monotonic():
                continue
            with self.store.lock(event["job_id"]):
                # Retry/reconciliation cannot change attempts during enqueue.
                record = self.store.get(event["job_id"])
                latest = self.store.event(event["event_id"])
                if (record is None or record["active_attempt_id"] != event["attempt_id"]
                        or latest is None or latest["state"] not in DELIVERABLE_STATES):
                    continue
                if (latest['kind'] in UNCERTAIN_STATES and latest['state'] in {'pending', 'archived'}
                        and self.verification_age(record, latest) < VERIFICATION_GRACE_SEC):
                    continue
                previous_state = latest["state"]
                try:
                    if (latest['kind'] in UNCERTAIN_STATES and latest['state'] in {'pending', 'archived'}
                            and record.get('job_kind', 'gpu') == 'gpu'
                            and not self.server._stale_policy_refusal()):
                        # The monitor and delivery run independently. A result
                        # may have become readable since the failed check.
                        # Verify once more before waking the owner with that
                        # older observation, using the same identity/lease rules.
                        with self.acting_as(Origin(record['origin_thread_id'])), self.operation(record):
                            self.server._manage_gpu_job_impl(action='status', job_id=record['job_id'],
                                early_poll_reason='Verify the result before delivering an uncertainty alert.')
                        latest = self.store.event(event['event_id'])
                        if latest is None or latest['state'] not in DELIVERABLE_STATES:
                            continue
                    result = self.daemon.deliver(latest, self.server.REPO_ROOT, before_send=lambda:
                        self.store.delivery(event["event_id"], "sending"))
                    if result == 'pending' and latest['kind'] == 'poll':
                        self._delivery_after[event['event_id']] = time.monotonic() + 5
                        continue
                    self.store.delivery(event["event_id"], result,
                                        error="delivery acknowledgement is uncertain" if result == "uncertain" else None)
                    self.health["delivery"] = "ready" if result == "accepted" else result
                except DeliveryUncertain as exc:
                    self.store.delivery(event["event_id"], "uncertain", error=str(exc)[:1000])
                    self.health["delivery"] = "uncertain"
                except Exception as exc:
                    self.store.delivery(event["event_id"], previous_state, error=str(exc)[:1000])
                    self.health["delivery"] = str(exc)[:300]
                self._delivery_after[event["event_id"]] = time.monotonic() + 60

    def start(self):
        for name, function, delay in [("monitor", self.monitor_once, 1), ("delivery", self.deliver_once, 5)]:
            def run(function=function, name=name, delay=delay):
                while not self._stop.wait(delay):
                    try:
                        function()
                    except Exception as exc:
                        self.health[name] = str(exc)[:500]
            thread = threading.Thread(target=run, name="gpu-mcp-" + name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self.daemon.close()

    def snapshot(self):
        # Human-only HTTP interface; never an MCP tool or model context.
        jobs = self.store.records()
        monitoring = {}
        with self._observation_lock:
            for record in jobs:
                identity = (record['job_id'], record['active_attempt_id'])
                observation = dict(self._monitor_observations.get(identity, {}))
                observation['lease'] = self.server.HEARTBEAT_MANAGER.diagnostics(*identity)
                monitoring[record['job_id']] = observation
        return {"repo_root": str(self.server.REPO_ROOT), "server_instance_id": self.server.SERVER_INSTANCE_ID,
                "health": self.health, "jobs": jobs, "events": self.store.events(), 'monitoring': monitoring}
