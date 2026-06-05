# ADR 0004p: Per-Task Heartbeats and Lease Cadence

## Status

Proposed. First draft.

This ADR defines the heartbeat model assumed by ADR 0004. The goal is not just to keep reservation metadata fresh. The goal is to let autonomous agents manage long GPU tasks without double-booking GPUs, wasting context on pointless polling, or idling unnecessarily when independent work is available.

## 1. Why This Exists

ADR 0004 defines the reservation invariant:

> A reservation may be cleaned up by an observer only when the owner's heartbeat is stale and remote process inspection proves the original job is gone.

That leaves two questions:

1. What heartbeat makes a reservation lease fresh or stale?
2. How should an agent decide when to check a long-running task again?

These are the same per-task mechanism viewed from two sides. A GPU job needs one deterministic task heartbeat. Each heartbeat write renews the reservation lease. Launch/status responses and hook reminders use the same recorded cadence to give the agent a reasoned opportunity to check progress, continue waiting, retry, finish, report, or resume independent work that does not depend on the job's outputs.

## 2. The Per-Task Heartbeat

The heartbeat is per managed GPU job, not per agent. If one agent manages two GPU jobs, each job has its own heartbeat interval, status, and next-check guidance.

The per-task heartbeat serves two related roles.

For the reservation protocol, it must:

- tell other MCP servers that this reservation still has a live owner;
- make stale-owner cleanup possible without a daemon;
- keep a long-running job reserved as long as its owner is healthy.

For agent behavior, it should:

- prevent agents from forgetting long-running jobs;
- prevent agents from burning tokens polling too often;
- teach the agent that output-dependent work is blocked until the job is terminal;
- allow independent work to continue while the GPU job runs;
- give each task its own cadence based on expected runtime and observed progress.

The heartbeat is deterministic. Its interval is calculated when the job is launched and may be revised after status checks as the runtime estimate or observable progress changes. It should not depend on ad hoc agent memory like "remember to check later"; the next check time should be explicit in tool output and job state. It is a recorded obligation to return to the task, not an instruction to block the whole agent.

If the owning MCP server dies, the task heartbeat stops. That fits ADR 0004: a stale heartbeat is evidence that the owner may be absent, but it is still not proof that the GPU is free. Observers must inspect the remote process before cleanup.

## 3. Decision

GPU MCP will treat every managed background job as a per-task heartbeat lease.

When `run_python_on_gpu` launches a job, the server creates a reservation and records:

- the owning `server_instance_id`, as defined by ADR 0004;
- process identity, as required by ADR 0004;
- a heartbeat cadence used for both reservation freshness and task status checks;
- enough persisted repo-local job state to retrieve logs and results through `manage_gpu_job(status)`.

Only the MCP server instance whose `server_instance_id` owns the reservation may write that task's heartbeat. A later MCP server instance may inspect, report, and clean up stale-gone reservations under ADR 0004, but it must not renew heartbeats for a reservation created by a prior instance. If persisted repo-local job state is missing, recovery can still find and report the reservation through shared metadata, but result/log retrieval may be limited until the user or agent supplies the missing output context.

The heartbeat updates shared reservation metadata (`last_heartbeat_at` and `heartbeat_interval_sec`) and exposes agent-facing guidance through launch and status responses.

Heartbeat writes must cooperate with ADR 0004's cleanup finalization guard. A heartbeat writer serializes same-process owner writes first, then briefly takes the cleanup finalization guard around the metadata re-read/merge/replace. Observer cleanup takes the same finalization guard only after remote inspection, then re-reads and renames under that guard. The guard is only for the final heartbeat-vs-quarantine window; ordinary heartbeat cadence remains per task and does not become a scheduler or daemon.

## 4. Cadence Principles

The cadence must be workload-sensitive.

A smoke probe that should finish in 30 seconds should be checked soon. A simulation expected to run for 8 hours should not make the agent poll every few minutes. The system should derive cadence from:

- explicit user expectation, when available;
- smoke-test or preflight timing, when the probe is explicitly representative or paired with an expected main-job duration;
- early progress observations;
- explicit progress or phase signals from the user, agent, status response, or
  job output only when the job has an agreed progress signal format;
- conservative min/max clamps.

These inputs are explicit signals, not hidden semantic inference from `ps` or `nvidia-smi`. The MCP server should not guess that a process is "warming up" or "checkpointing" from utilization patterns in v1.

Smoke and preflight jobs are explicit evidence, not magic inference. Before a
real long-running job, the agent should run a short smoke/preflight job when
feasible. That smoke run has two purposes:

- run viability: script path, environment, imports, arguments, output path, and
  GPU access work on the selected host;
- runtime estimate: the agent gets evidence for the first heartbeat cadence of
  the main job.

The MCP server cannot invent smoke arguments for arbitrary scripts. The agent or
human must supply representative smoke arguments, or the agent must explicitly
record why smoke was skipped.

Smoke is not a separate template/config subsystem. A smoke test is a normal
managed GPU job launched through `run_python_on_gpu` with `job_role="smoke"`.
Its evidence is the ordinary managed job record, outcome record, output pointer,
runtime, and `job_id`. Phase 7 must not add repo-local smoke template files,
script-to-args recipe maps, or a second smoke-only launch path. The agent is
responsible for choosing representative smoke arguments and for interpreting
whether the smoke result is representative of the main job.

The smoke `job_id` is the Phase 7 linkage key. A main launch that supplies
`smoke_job_id` gives the server all indexing information needed for the normal
workflow: look up that repo-local smoke job record, validate its role and
terminal outcome, read its runtime and output pointers, and record it in
`cadence_basis`. Listing or searching older smoke jobs is a separate recovery
convenience and is not part of the core Phase 7 contract.

Runtime estimates are guidance, not authority. They may affect heartbeat interval and suggested next poll time. They must not be used as proof that a reservation is free. Cleanup still follows ADR 0004: stale task heartbeat plus process proof.

Heartbeat interval revisions must be written with a fresh heartbeat. The server must not shorten or lengthen `heartbeat_interval_sec` without also updating `last_heartbeat_at` in the same metadata write. Stale computation therefore uses the interval stored with the last successful heartbeat write; a shortened interval must not retroactively make an older heartbeat stale.

For v1, the fixed-cadence defaults are:

| Constant | Value | Meaning |
|----------|-------|---------|
| default heartbeat interval | `600` seconds | Used when launch has no better task-specific cadence. Also used for the first suggested status check in the fixed-cadence implementation. |
| minimum heartbeat interval | `60` seconds | Prevents accidental high-frequency writes and status reminders. |
| maximum heartbeat interval | `3600` seconds | Prevents stale detection from being delayed indefinitely, even after dynamic cadence exists. |
| stale multiplier | `3` | A reservation is stale when `now - last_heartbeat_at > heartbeat_interval_sec * 3`. |

These are design constants, not user-facing scheduler policy. They can become configuration later if real deployments need it, but v1 should keep them fixed so behavior is predictable and tests are stable. Dynamic cadence may later choose any per-task interval inside the min/max range; cleanup still requires stale heartbeat plus process proof.

## 5. Tool Behavior

Launch responses should return promptly with a managed job handle and a suggested first status check. They should not wait for the GPU job to finish.

Status responses should report:

- current computed reservation state;
- job lifecycle, when known, distinct from reservation diagnostics;
- whether the current server owns the lease;
- process status and recent inspection result;
- current-repo result/log pointers or a bounded stdout/stderr tail when appropriate;
- agent guidance derived from job lifecycle and reservation diagnostics;
- suggested next poll time or poll interval.

If the job lifecycle is running or retrying, or if reservation diagnostics are unclear because inspection failed, tool output should tell the agent not to invent dependent work. It may continue independent work before the next suggested check. If no independent work is available, waiting idly and polling is acceptable behavior. If the job is terminal, status should distinguish success, failure, and process-gone-with-unknown-outcome, then tell the agent what outputs are ready and what lifecycle actions are valid.

In the fixed-cadence v1 path, `next_poll_after` is `now + heartbeat_interval_sec` after launch or status unless the job is terminal. Dynamic cadence can later revise this based on expected duration or progress evidence, but it must stay within the min/max interval bounds above.

Phase 7 adds public cadence evidence to `run_python_on_gpu`. The launch input
should accept:

- `job_role`: one of `smoke`, `main`, or `one_off`;
- `expected_duration_sec`: an optional explicit expected runtime for the
  launched job;
- `cadence_hint_sec`: an optional direct heartbeat cadence request;
- `smoke_job_id`: an optional managed smoke job whose viability/timing is being
  used as evidence for a main job;
- `smoke_skip_reason`: an optional reason a likely main/long job is being
  launched without smoke evidence.

If `job_role` is omitted, explicit launch semantics provide only a default role,
not proof of runtime. The default should be:

- `main` when `async_mode=True`;
- `one_off` when `async_mode=False`, because `async_mode=False` is retained as
  compatibility input even though it still returns a managed job handle.

Launch output must include the resolved `job_role` and whether it was defaulted.
An explicit `job_role` always wins over the default.

For `job_role="main"`, launch should soft-refuse when there is no smoke evidence
or explicit opt-out evidence: no `smoke_job_id`, no `expected_duration_sec`, no
`cadence_hint_sec`, and no `smoke_skip_reason`. The refusal is a workflow guard,
not a GPU safety boundary. It should tell the agent to either run a smoke job
first or retry with a concrete `smoke_skip_reason`.

If `smoke_job_id` is supplied, the server must validate it before using it as
cadence evidence. It must identify a same-repo managed job with
`job_role="smoke"` and a successful terminal outcome. The server still does not
prove representativeness; it records that the agent or human chose to use that
smoke result as evidence.

Launch output must expose the selected `heartbeat_interval_sec`,
`next_poll_after`, and `cadence_basis`. The cadence basis should be structured
enough to say whether the cadence came from a smoke job, explicit expected
duration, direct cadence hint, or the default cadence, and should include
human-readable guidance such as "based on smoke job X" or "default cadence used;
no smoke evidence".

During polling, the agent may discover the initial cadence is wrong. Phase 7
therefore adds an owner-side lifecycle action:
`manage_gpu_job(action="update_cadence", job_id=..., expected_duration_sec=...,
cadence_hint_sec=..., reason=...)`. The server clamps the selected interval to
`60..3600` seconds and updates `heartbeat_interval_sec` and `last_heartbeat_at`
in the same metadata write. This action records the agent's revised cadence
judgment; it does not infer progress from arbitrary output.

When `manage_gpu_job(status)` observes that a smoke job has succeeded, status
output should make the smoke-to-main transition explicit. It should preserve the
smoke `job_id`, report the observed runtime when available, and guide the agent
to launch the main job with `job_role="main"` and
`smoke_job_id="<smoke job id>"` if the agent judges the smoke result
representative. This keeps smoke evidence discoverable through durable job state
instead of relying on transient chat memory.

## 6. Implementation Posture

The initial implementation must not require users to launch Codex with app-server, remote-control, or other extra command-line arguments.

The core heartbeat should be implemented inside the GPU MCP server as per-task lease state and repo-local job state. Tool outputs and existing Codex hook surfaces may expose due or overdue status checks to the agent. This realizes the safety invariant and the normal agent workflow without a separate gateway.

The hook reminder path is client-specific. Codex `PreToolUse` with `additionalContext` is the intended first client integration. Other MCP clients may lack an equivalent context-injection hook; for them, the design degrades to launch/status tool output and ordinary agent memory. This is a known v1 usability limitation, not a reservation-safety limitation.

Codex app-server or remote-control may later provide stronger scheduled reminders by starting a status-check turn in an existing Codex thread. That is an optional enhancement, not a dependency of the heartbeat design, until it is stable and can be installed without per-session user friction.

## 7. Hook Reminder Path

The v1 reminder path should use the existing Codex hook surface, not app-server.
The hook is installed as a user-global GPU MCP companion hook. It is safe as a
global hook because it first discovers the current repo by walking upward from
the hook working directory to the nearest `gpu-mcp.toml`; if no policy is found,
it exits quietly.

The MCP server records per-task reminder state in repo-local job state:

- `next_poll_after`: when the agent should next check this job;
- `last_status_checked_at`: the last real `manage_gpu_job(status)` check.

Hook-delivery deduplication is recorded in separate hook-owned advisory state, not in the MCP server's main job record. For example, the hook may write `<repo>/.gpu_mcp_state/hook_reminders/<job_id>.json` with `last_hook_reminded_poll_after`. This avoids lost updates between the hook process and the MCP server process.

A `PreToolUse` hook may read this state before the agent's next tool call. The
reminder scan is scoped to the current repo's
`<repo>/.gpu_mcp_state/jobs/` directory. If a job is due and `next_poll_after` is
newer than the hook-owned `last_hook_reminded_poll_after`, the hook injects a
small `additionalContext` reminder telling the agent to call
`manage_gpu_job(status)` when appropriate, avoid output-dependent work, and
continue only with independent work until the job is terminal. The hook then
records in its advisory state that this due timestamp was reminded, so normal
tool use is not spammed with the same reminder.

Before reminding, the hook should cheaply confirm that the local job record still
corresponds to an existing reservation with the same `job_id`,
`reservation_key`, and recorded `server_instance_id`. If the shared reservation
is gone, disappears during read, or no longer matches the local job record, the
hook should suppress the reminder and retire the local advisory reminder state.
If the reservation is already known locally but is not owned by the current live
server instance, the reminder should be status-only; it must not suggest
`retry`, `finish`, or other owner-side lifecycle actions. The hook must not scan
the shared registry, emit reminders for another repo's jobs, or create a full
managed job record merely to produce a reminder. This synchronization check is
local filesystem work only; it is not remote process inspection.

The hook must be cheap and local. It must not SSH to GPU hosts, inspect remote processes, call MCP tools, or decide whether a reservation is free. It is a behavioral reminder layer only.

Missing a reminder is acceptable. If the agent is between tool calls, stuck thinking, waiting for user input, or otherwise not invoking tools, the hook does not fire. That does not break the design: the MCP-owned heartbeat and ADR 0004 cleanup invariant remain the safety boundary. The next tool use can surface the reminder if it is still relevant.

For Phase 7, the hook's cadence role is behavioral only. Before a
`run_python_on_gpu` call that is explicitly or defaultedly `job_role="main"` and
has no `smoke_job_id`, `expected_duration_sec`, `cadence_hint_sec`, or
`smoke_skip_reason`, the hook should inject a structured precondition: the agent
must either launch a representative `job_role="smoke"` job first or retry the
main launch with a concrete `smoke_skip_reason`. When a running job is due, the
hook may remind the agent to status-check the job and update cadence if observed
progress contradicts the old cadence. The hook must not act as a scheduler and
must not write heartbeats or cadence state itself.

## 8. Failure Boundaries

Heartbeat failure and late polling have different meanings.

If the owner cannot maintain a task heartbeat, including because shared metadata writes are failing, it cannot safely perform owner-side lifecycle mutations such as launch, retry, stop, or finish. Read-only status and explicitly fingerprint-gated rescue actions follow ADR 0004's rules.

If the agent misses a suggested check but the owner's heartbeat is still fresh, that is not a lease failure. The next status call should simply report the current state and provide updated guidance.

If the task heartbeat is stale, observers may inspect the remote process. They still must not clean up the reservation unless process proof shows the original job is gone or has a different identity.

## 9. What This ADR Will Specify Later

This draft intentionally keeps formula-level policy simple. The final version should specify:

- edge-case validation and precedence rules not fixed above when multiple
  public cadence inputs are supplied;
- how dynamic `next_poll_after` is computed beyond direct `cadence_hint_sec`
  clamping;
- how the cadence basis is stored in repo-local job state while shared
  reservation metadata remains limited to lease fields;
- heartbeat lifecycle and health states beyond the initial `healthy`/`unhealthy` boundary;
- the exact `PreToolUse` hook output shape and fallback behavior for Codex versions without `additionalContext`;
- the exact atomic file-write mechanics for hook-owned advisory reminder state;
- the exact cleanup policy for stale hook-owned advisory files after the shared reservation is gone;
- whether app-server scheduled turns are supported as an optional enhancement;
- tests for per-task cadence, stale detection, missed task checks, hook reminder delivery, and heartbeat writer failure.

## 10. Rejected Directions

### Ad Hoc Agent Memory As Heartbeat

Rejected. The heartbeat cannot be an implicit instruction like "remember to check later." It must be an explicit per-task cadence recorded in job state and reflected in tool output. If the owner dies, the heartbeat stops and ADR 0004's stale-plus-process-proof rule takes over.

### Per-Agent Heartbeat Only

Rejected. One agent may manage multiple GPU tasks with different expected runtimes. Cadence belongs to the task, not the agent process.

### Fixed Polling Interval

Rejected. A single polling interval either wastes tokens on long jobs or reacts too slowly to short probes.

### Smoke Template Files

Rejected. Smoke arguments are workload judgment, not MCP policy. A repo-local
template or recipe file would add a second configuration surface, create path
and argument-substitution rules, and still not prove representativeness. Phase 7
uses normal managed GPU jobs for smoke tests and durable job records for smoke
evidence.

### Runtime Estimate as Cleanup Authority

Rejected. Estimates can guide polling and heartbeat cadence, but they cannot prove a GPU is free. Cleanup authority remains the ADR 0004 two-condition rule.
