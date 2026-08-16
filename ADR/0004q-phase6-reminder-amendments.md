# ADR 0004q: Phase 6 Reminder Amendments

## Status

Proposed amendment.

This document amends the Phase 6 hook reminder path described in
`progress-004.md`, ADR 0004p, and the ADR 0004 implementation companion. It only
records the narrow behavior changes needed before implementing Phase 6.

## 1. Throttled Re-Reminders Until Status Acknowledgement

The Phase 6 reminder path must not permanently suppress a reminder just because
the hook already emitted once for the current `next_poll_after`.

A reminder for a due managed job is acknowledged only when one of these happens:

- `manage_gpu_job(action="status", job_id=...)` performs a full status check
  for that job;
- the job reaches terminal status;
- the local job record no longer matches an active shared reservation and the
  hook retires its advisory state.

ADR 0004p Phase 7 adds a compact early-poll status mode,
`polling_state="not_due_yet"`, for status calls before `next_poll_after` without
an explicit `early_poll_reason`. That compact response is not a full status
check and must not acknowledge or silence a due reminder. A due status check or
an intentional early override with `early_poll_reason` uses the full status path
and may acknowledge the reminder normally.

Until acknowledged, the hook may re-remind for the same `next_poll_after`, but
only after a time throttle. The v1 throttle is:

- suppress immediate duplicate reminders for the same due timestamp;
- allow a repeat reminder only after at least one repo-local polling interval has elapsed
  since the last reminder for that job.

The throttle interval comes from the matching repo-local job record's
`poll_interval_sec`. If that field is missing, malformed, boolean, or
non-positive, use ADR 0004p's one-hour default polling interval. A valid positive
value is used exactly, with no policy maximum. Shared reservation
`heartbeat_interval_sec` is deliberately not consulted: lease renewal frequency
must not determine how often the agent is reminded to perform a status check.

The hook-owned advisory state should therefore track, per job:

- `last_reminded_poll_after`;
- `last_reminded_at`;
- `reminder_count_for_poll_after`.

`last_status_checked_at` remains MCP-server-owned job state. The hook must read
it to decide acknowledgement, but must not write it.

Reminders are evaluated only from Codex `PreToolUse`. `PostToolUse` remains
silent for the reminder branch.

For installed Codex hooks, `additionalContext` is the default Phase 6 reminder
mode. An explicit `GPU_MCP_HOOK_REMINDER_MODE=off` may disable reminders for
diagnostics or emergency quieting; requiring an opt-in environment variable is
not part of v1.

## 2. Multiple Due Jobs in One Reminder

If several current-repo managed jobs are due, the hook should emit one compact
reminder that lists the due jobs together instead of choosing only one job.

The reminder should include each due job's `job_id` and how overdue it is. It
should not decide the science. The agent can still use the current job state and
durable intermediate evidence to choose what to do.

There is no v1 priority queue, severity scoring, or cross-job scheduling policy.
If a deterministic order is needed, sort by oldest `next_poll_after` first, then
by `job_id`.

Throttle and acknowledgement state remain per job, not global. A status check
for job A must not acknowledge or silence job B.

The hook emits `hookSpecificOutput.additionalContext`. For one due job, the
context should be compact. This is a synthetic example:

```text
GPU MCP: 1 managed job has a scheduled status check due.
- <job-id>: scheduled status check is overdue by 5m; call manage_gpu_job(action="status", job_id="<job-id>").
Continue from the returned lifecycle.
```

For multiple due jobs, emit one context block. Again, the IDs are placeholders:

```text
GPU MCP: 2 managed jobs have scheduled status checks due.
- <job-id-a>: scheduled status check is overdue by 18m; call manage_gpu_job(action="status", job_id="<job-id-a>").
- <job-id-b>: scheduled status check is overdue by 16m; call manage_gpu_job(action="status", job_id="<job-id-b>").
Continue from the returned lifecycle.
```

## 3. `finish` Must Refuse While the Process Is Live

`manage_gpu_job(action="finish")` must not abandon a still-live owned process as
the default behavior.

When `finish` inspects the recorded process:

- if the matching process is gone, `finish` may remove the reservation and retire
  the job as already specified;
- if the matching process is still alive, `finish` must refuse, keep the
  heartbeat active, and tell the agent to call `stop` first if it intends to
  terminate the job, or keep polling `status` if it intends to wait.

No new `surrender_live` or abandon parameter is part of v1.

This is deliberate workflow friction. If the agent wants to give up on a live
job and free the GPU, it must explicitly call `stop` first, then use `finish`
after the matching process is gone. `finish` never implicitly terminates or
abandons a live process in v1.

## 4. Accepted V1 Limitation: No-Tool-Turn Blind Spot

Phase 6 reminders are Codex `PreToolUse` triggered. If the agent does not make a
tool call that fires `PreToolUse`, no Phase 6 reminder fires.

This is accepted for v1. The next `PreToolUse` event may surface any
still-relevant due reminders. Phase 6 does not add `SessionStart`,
`UserPromptSubmit`, app-server, or remote-control reminder delivery.

## 5. Test Amendments

Phase 6 tests should amend the existing reminder cases as follows:

- update `test_phase6_due_reminder_emits_additional_context` to assert the
  concrete one-job context shape above;
- assert reminders default to `additionalContext`, with only an explicit `off`
  mode disabling them;
- replace `test_phase6_due_reminder_deduplicates_same_due_timestamp` with a
  throttle-window test: same due timestamp is silent before one polling
  interval has elapsed;
- add a same-due-timestamp re-reminder test after one repo-local
  `poll_interval_sec` when no status acknowledgement occurred;
- test the one-hour malformed/missing fallback and a direct interval longer than
  the former heartbeat maximum;
- add a status-acknowledgement test: `manage_gpu_job(status)` for job A updates
  `last_status_checked_at` and silences job A's old due timestamp only when it
  performs the full status path;
- add a compact early-status test: `polling_state="not_due_yet"` does not update
  `last_status_checked_at` and does not silence the later reminder;
- add a per-job acknowledgement test: status for job A must not acknowledge or
  silence job B;
- add a multi-job context test: one `PreToolUse` context can list multiple due
  current-repo jobs in deterministic order;
- keep `test_phase6_future_poll_and_posttooluse_are_silent` and extend it, if
  needed, so `PostToolUse` remains silent even when due jobs exist;
- update Phase 5 lifecycle tests so `finish` refuses while the matching process
  is alive and keeps heartbeat ownership active.
