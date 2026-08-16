# ADR 0005: Prompt Status From Managed-Job Events

## Status

Accepted and implemented on 2026-08-07. Compatibility with the independent
polling model was clarified on 2026-08-16.

## Context

The managed-job lifecycle has one central invariant:

> `status` discovers completion, tells the agent, and releases the reservation
> in one operation.

The launcher already writes a durable `outcome.json`. Before this decision,
`PreToolUse` prompted `status` only when `next_poll_after` was due. It did not
notice an outcome written before that time. An agent could therefore keep using
tools without learning that its job had already ended. Once the agent stopped
working, no hook remained active to prompt the eventual status check.

Moving outcome handling into the background heartbeat would break the
invariant. Background finalization would then require undelivered-result
records, delivery deduplication, ordering between job updates and reservation
removal, and recovery from failed notification delivery.

## Decision

The hooks recognize one shared readiness condition for an owned, nonterminal
job in the current repository:

- the active attempt's `outcome.json` exists; or
- `next_poll_after` has arrived.

They check file presence only. They do not parse `outcome.json`.

During active work, `PreToolUse` checks this condition before other advisory
GPU guidance and adds context directing the agent to call `status`. Outcome
and cadence reminders are throttled independently, so a recent cadence reminder
cannot hide a newly written outcome. Re-reminder timing comes from the
repo-local job's `poll_interval_sec`, not shared heartbeat metadata.

At the end of a turn, `Stop` waits locally for the same condition. When one
becomes ready, it returns a continuation prompt directing the agent to call
`status`. Waiting consumes no model turns and requires no additional daemon.
This suspends the current interactive turn; it does not wake a closed Codex
session.

The Stop hook's one-second local scan interval is only a file/timestamp
observation frequency so an early `outcome.json` is noticed promptly. It is not
an MCP status poll and does not change `next_poll_after`. A three-hour
`poll_interval_sec`, for example, can leave Stop suspended until that time while
the MCP heartbeat manager independently renews the reservation.

Codex command hooks require a finite runner timeout. Installation guidance uses
`31536000` seconds as a one-year operational watchdog rather than the former
roughly one-hour value. This watchdog is not passed to cadence selection and is not
an MCP polling-policy maximum. Deployments intentionally suspending a turn for
longer than the watchdog must raise it; interruption still leaves job and
reservation state unchanged.

`manage_gpu_job(action="status")` remains the only operation that:

- interprets the outcome;
- reports the result to the agent;
- changes terminal job state;
- stops the reservation heartbeat; and
- releases the GPU.

## Intermediate Outputs

Terminal status is required for final-result claims and lifecycle actions, not
for every read of application output. While a job is running, an agent may
analyze an artifact read-only when the application has already closed it or
published it atomically. Such analysis must be described as provisional.

GPU MCP does not infer durability for arbitrary scientific formats. The job's
own output protocol defines the safe boundary, such as a closed HDF5 chunk or
an atomically renamed checkpoint. Open or mutable files are not covered.

If no outcome exists at the scheduled time, `status` retains its existing
process-identity inspection. A failed remote inspection remains unknown and
does not release the reservation.

## Heartbeat Responsibility

The heartbeat is unchanged and independent of `poll_interval_sec`. It renews
reservation ownership while the managed job is active and during the handoff
between outcome creation and `status`.
Without it, a healthy long job could appear stale and its GPU could be claimed
again.

Hooks are notification mechanisms, not lease managers. Hook interruption,
timeout, malformed outcome content, or failed delivery leaves job and
reservation state unchanged.

## Event Scope

Both hooks require matching repository, job, active attempt, reservation key,
and server instance records. Terminal or unreserved jobs are ignored.

`PreToolUse` stores only reminder-throttle state. `Stop` stores only a
discardable session/turn event key to prevent an immediate duplicate
continuation. Neither is a durable result queue.

## Rejected Alternative

External app-server turn injection was rejected because checking thread state
and calling `turn/start` are not atomic. It can race with an active interactive
turn. Reconsider it only if Codex provides an atomic start-if-idle or queued
delivery operation.

## Anti-Patterns

- Parsing or finalizing outcomes in the heartbeat or hooks.
- Releasing a reservation before `status` delivers the terminal result.
- Treating absence of `outcome.json` as proof that the process died.
- Calling MCP lifecycle or remote-inspection operations from a hook.
- Creating a second durable notification lifecycle.
- Polling with model turns while a local Stop hook can wait.

## Verification

Contract tests cover active outcome detection, independent throttling,
repository and ownership scope, Stop continuation deduplication, and
nonmutation by hooks. Interactive live results are recorded in
`test/ADR0005_INTERACTIVE_HOOK_RESULTS.md`.

## References

- [Codex hooks](https://learn.chatgpt.com/docs/hooks)
- [Codex app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
