# ADR 0004p: Per-Task Heartbeats and Agent Polling Cadence

## Status

Accepted and implemented. Amended on 2026-08-16 to separate reservation
heartbeats from agent polling cadence.

This ADR defines the heartbeat model assumed by ADR 0004. The goal is not just to keep reservation metadata fresh. The goal is to let autonomous agents manage long GPU tasks without double-booking GPUs, wasting context on pointless polling, or idling unnecessarily when independent work is available.

## 1. Why This Exists

ADR 0004 defines the reservation invariant:

> A reservation may be cleaned up by an observer only when the owner's heartbeat is stale and remote process inspection proves the original job is gone.

That leaves two questions:

1. What heartbeat makes a reservation lease fresh or stale?
2. How should an agent decide when to check a long-running task again?

These are separate timing systems. Heartbeat writes protect reservation
ownership. Repo-local polling state tells the agent when another lifecycle check
is useful. Changing an agent's polling schedule must not lengthen or shorten
stale-owner detection.

## 2. The Per-Task Heartbeat

The heartbeat is per managed GPU job, not per agent. If one agent manages two
GPU jobs, each job has its own lease heartbeat. Polling guidance is stored
separately with each repo-local job record.

The per-task heartbeat serves two related roles.

For the reservation protocol, it must:

- tell other MCP servers that this reservation still has a live owner;
- make stale-owner cleanup possible without a daemon;
- keep a long-running job reserved as long as its owner is healthy.

The separate polling schedule should:

- prevent agents from forgetting long-running jobs;
- prevent agents from burning tokens polling too often;
- allow read-only, provisional analysis of outputs that the application has
  already closed or atomically published;
- reserve final-result claims and lifecycle decisions for terminal status;
- allow other independent work to continue while the GPU job runs;
- let the agent choose a task-specific cadence explicitly.

Polling is deterministic once chosen. `cadence_hint_sec` and the resulting
`next_poll_after` are explicit in tool output and job state; they do not depend
on ad hoc memory like "remember to check later." The hint is a recorded
opportunity to return to the task, not an instruction to block the whole agent.

If the owning MCP server dies, the task heartbeat stops. That fits ADR 0004: a stale heartbeat is evidence that the owner may be absent, but it is still not proof that the GPU is free. Observers must inspect the remote process before cleanup.

## 3. Decision

GPU MCP will treat every managed background job as a per-task heartbeat lease.

When `run_python_on_gpu` launches a job, the server creates a reservation and records:

- the owning `server_instance_id`, as defined by ADR 0004;
- process identity, as required by ADR 0004;
- a lease-heartbeat cadence used only for reservation freshness;
- a repo-local polling interval and `next_poll_after` used for task status checks;
- enough persisted repo-local job state to retrieve logs and results through `manage_gpu_job(status)`.

Only the MCP server instance whose `server_instance_id` owns the reservation may write that task's heartbeat. A later MCP server instance may inspect, report, and clean up stale-gone reservations under ADR 0004, but it must not renew heartbeats for a reservation created by a prior instance. If persisted repo-local job state is missing, recovery can still find and report the reservation through shared metadata, but result/log retrieval may be limited until the user or agent supplies the missing output context.

The heartbeat updates shared reservation metadata (`last_heartbeat_at` and
`heartbeat_interval_sec`). Polling guidance is repo-local and is exposed through
launch and status responses as `poll_interval_sec` and `next_poll_after`.

Heartbeat writes must cooperate with ADR 0004's cleanup finalization guard. A heartbeat writer serializes same-process owner writes first, then briefly takes the cleanup finalization guard around the metadata re-read/merge/replace. Observer cleanup takes the same finalization guard only after remote inspection, then re-reads and renames under that guard. The guard is only for the final heartbeat-vs-quarantine window; ordinary heartbeat cadence remains per task and does not become a scheduler or daemon.

## 4. Cadence Principles

Polling should be workload-sensitive, but the agent—not a server-side duration
table—chooses it. `cadence_hint_sec` is a direct positive integer number of
seconds and has no policy maximum. A fast task may request a few seconds; a long
task may request many hours.

`expected_duration_sec`, observed smoke runtime, and progress observations are
descriptive evidence the agent may use when choosing or revising
`cadence_hint_sec`. They never silently alter the schedule. The MCP server also
does not infer cadence from comments, `ps`, `nvidia-smi`, or arbitrary output.

Smoke and preflight jobs are explicit evidence, not magic inference. Before a
real long-running job, the agent should run a short smoke/preflight job when
feasible. That smoke run has two purposes:

- run viability: script path, environment, imports, arguments, output path, and
  GPU access work on the selected host;
- runtime estimate: the agent gets evidence it may use to choose the main job's
  explicit polling cadence.

The MCP server cannot invent smoke arguments for arbitrary scripts. The agent
must derive or create a representative smoke path from the task context, code,
script options, CLI help, config, existing test conventions, tiny input
fixtures, or code it adds for smoke execution. If it truly cannot identify a
useful smoke path, it must explicitly record why smoke was skipped.

`preflight` is descriptive prose in this ADR. The public `job_role` wire value
for these jobs is `smoke`.

Smoke is not a separate template/config subsystem. A smoke test is a normal
managed GPU job launched through `run_python_on_gpu` with `job_role="smoke"`.
Its evidence is the ordinary managed job record, outcome record, output pointer,
runtime, and `job_id`. Phase 7 must not add repo-local smoke template files,
script-to-args recipe maps, or a second smoke-only launch path. The agent is
responsible for choosing or creating the smoke path and for interpreting whether
the smoke result is representative of the main job.

A smoke job does not have to be the exact main script with smaller arguments.
It may be the target script with reduced flags, a smoke mode the agent adds, a
small input fixture, a dry-run or max-steps path, or a separate managed smoke
script that imports and exercises the same relevant GPU code path. The harness
should not confine this choice. The only mandatory part is that the smoke run
goes through the same GPU MCP launch path and produces a durable managed
`job_id`.

Smoke jobs still use the normal reservation and heartbeat protocol. A smoke job
can hang, wedge during CUDA initialization, or run longer than intended, so it
must not be heartbeat-free while it holds a GPU reservation. Without an explicit
`cadence_hint_sec`, a smoke launch uses a five-minute polling fallback. The agent
may request a faster or slower interval.

The smoke `job_id` is the Phase 7 linkage key. A main launch that supplies
`smoke_job_id` gives the server all indexing information needed for the normal
workflow: look up that repo-local smoke job record, validate its role, read its
lifecycle/outcome, runtime, and output pointers, and record those facts in
`cadence_basis`. Listing or searching older smoke jobs is a separate recovery
convenience and is not part of the core Phase 7 contract.

Runtime estimates are guidance, not lease authority. They do not change polling
or prove that a reservation is free. Cleanup still follows ADR 0004: stale lease
heartbeat plus process proof.

For v1, timing constants are grouped by purpose:

| Group | Constant | Value | Meaning |
|-------|----------|-------|---------|
| lease | minimum heartbeat interval | `60` seconds | Lower validation bound for shared lease metadata. |
| lease | default heartbeat interval | `600` seconds | Shared lease expectation for new jobs. |
| lease | maximum heartbeat interval | `3600` seconds | Upper validation bound for shared lease metadata. It is not a polling cap. |
| lease | heartbeat-manager tick | `1` second | Local scheduler scan; it is not a metadata write or remote status check. |
| lease | heartbeat write cap | `60` seconds | Active ownership is renewed at least this often even when the lease interval is longer. |
| lease | stale multiplier | `3` | Default stale threshold is `600 * 3 = 1800` seconds after the last successful write. |
| polling | minimum representable interval | `1` second | Technical positive-integer floor, not a workload recommendation. |
| polling | smoke fallback | `300` seconds | Used only when a smoke job has no direct hint. |
| polling | main/one-off fallback | `3600` seconds | Used only when a non-smoke job has no direct hint. |
| polling | maximum interval | none | Direct hints are not policy-capped. |

Selection is therefore simple:

1. If `cadence_hint_sec` is present, use it exactly.
2. Otherwise use the role fallback: `300` seconds for smoke, `3600` seconds for
   main or one-off.

`expected_duration_sec` and smoke runtime remain recorded in `cadence_basis` but
are descriptive only. `manage_gpu_job(action="update_cadence")` requires a
direct `cadence_hint_sec`. `next_poll_after` is `now + poll_interval_sec` and is
returned as a UTC RFC 3339 timestamp ending in `Z`; terminal jobs omit it or
return null.

Changing polling refreshes `last_heartbeat_at` as an owner-liveness write but
does not change the reservation's `heartbeat_interval_sec`. Thus a three-hour
polling interval still uses the ordinary lease-safety timing.

## 5. Tool Behavior

Launch responses should return promptly with a managed job handle and a suggested first status check. They should not wait for the GPU job to finish.

Full status responses should report:

- current computed reservation state;
- job lifecycle, when known, distinct from reservation diagnostics;
- whether the current server owns the lease;
- process status and recent inspection result;
- current-repo result/log pointers or a bounded stdout/stderr tail when appropriate;
- agent guidance derived from job lifecycle and reservation diagnostics;
- suggested next poll time or poll interval.

If the job lifecycle is running or retrying, or if reservation diagnostics are
unclear because inspection failed, tool output should tell the agent not to
invent dependent work. It may continue independent work before the next
suggested check. If no independent work is available, waiting until the next
suggested check and then polling is acceptable behavior. If the job is terminal,
status should distinguish success, failure, and
process-gone-with-unknown-outcome, then tell the agent what outputs are ready
and what lifecycle actions are valid.

`next_poll_after` is `now + poll_interval_sec` after launch or full status unless
the job is terminal. A direct cadence hint changes `poll_interval_sec`; no other
evidence silently revises it.

Phase 7 also makes poll discipline an explicit user-experience contract. The
goal is to keep agents from burning context by repeatedly checking a long GPU
job before the MCP-provided cadence says the job is due. Tool output and hook
context should tell the agent not to call
`manage_gpu_job(action="status", job_id=...)` before `next_poll_after` unless
the user asks, the job output is now blocking the next step, or the agent has
another concrete reason.

`manage_gpu_job(action="status")` should accept an optional
`early_poll_reason`. Blank or whitespace-only values behave as absent. A status
call is due when the job has a parseable `next_poll_after` and
`now >= next_poll_after`.

When a targeted status call is before `next_poll_after`, has no
`early_poll_reason`, and the current repo-local job record is known nonterminal,
the server should return a compact local-only response instead of a full status
check. This response is a cadence-preserving advisory response, not a lifecycle
assertion. It must not imply the remote process is alive. It should include:

- `status: "ok"`;
- `polling_state: "not_due_yet"`;
- `job_id`;
- `reservation_key`, when known;
- `poll_interval_sec`;
- `next_poll_after`;
- `seconds_until_due`;
- `full_status_performed: false`;
- `remote_inspection_performed: false`;
- `log_tail_included: false`;
- concise guidance to continue independent work or retry with
  `early_poll_reason` if an immediate full check is justified.

The compact `not_due_yet` path must not SSH, inspect the remote process, tail
logs, update `last_status_checked_at`, advance `next_poll_after`, renew the
heartbeat, change cadence, update process-inspection state, or acknowledge a
hook reminder. In ADR 0004q terms, it is not a real status check.

The server must take the normal full status path instead when the status call is
due or overdue, `early_poll_reason` is non-empty, a local terminal outcome is
already known, `next_poll_after` is missing or malformed, the target is
ambiguous, or ownership/policy/recovery diagnostics require full status. A
non-empty `early_poll_reason` records an intentional early override in
repo-local state and/or tool output, then performs full status. If recording the
override fails, the server may still return full status, but the response should
include `early_poll_override_recorded: false` and a bounded warning because the
override record is observability, not a safety boundary.

Phase 7 adds public cadence evidence to `run_python_on_gpu`. The launch input
should accept:

- `job_role`: one of `smoke`, `main`, or `one_off`;
- `expected_duration_sec`: optional descriptive expected runtime for the
  launched job; it does not schedule polling;
- `cadence_hint_sec`: an optional direct positive polling interval with no
  policy maximum;
- `smoke_job_id`: an optional managed smoke job whose viability is being used as
  evidence for a main job;
- `smoke_cadence_representative`: a descriptive boolean recording the caller's
  judgment; it does not schedule polling;
- `smoke_skip_reason`: an optional reason a likely main/long job is being
  launched without smoke evidence.

If `job_role` is omitted, explicit launch semantics provide only a default role,
not proof of runtime. The default should be:

- `main` when `async_mode=True`;
- `one_off` when `async_mode=False`, because `async_mode=False` is retained as
  compatibility input even though it still returns a managed job handle.

Launch output must include the resolved `job_role` and whether it was defaulted.
An explicit `job_role` always wins over the default.

For `job_role="main"`, launch should soft-refuse when there is neither positive
smoke viability evidence nor explicit opt-out evidence. Expected duration and
cadence hints do not explain why smoke was skipped. If the launch has no
successful `smoke_job_id` and no
`smoke_skip_reason`, the refusal is a workflow guard, not a GPU safety boundary.
It should tell the agent to either run a smoke job first or retry with a
concrete `smoke_skip_reason`. If the agent retries with `smoke_skip_reason`, the
reason must be recorded in launch output and `cadence_basis`; the server should
reject absent or whitespace-only reasons, but it should not pretend that
syntactic reason validation proves smoke was truly infeasible.

If `smoke_job_id` is supplied, the server must validate it before using it as
evidence. It must identify a same-repo managed job with
`job_role="smoke"` and a readable lifecycle/outcome. The server still does not
prove representativeness or require success merely to record the reference; it
records that the caller chose to use that smoke result as evidence.

A valid `smoke_job_id` is smoke evidence by default. If the referenced smoke job
succeeded, launch output may cite it as positive viability evidence. If it
failed, is still running, or has an unknown outcome, launch output must record
that lifecycle and must not describe it as positive viability evidence. Smoke
runtime is descriptive evidence for the agent; the server never extrapolates a
polling interval from it.

For a `job_role="main"` launch with no direct cadence hint, including a launch that
uses only `smoke_skip_reason` or only a viability smoke job, the first
`poll_interval_sec` is the one-hour fallback. A smoke job without a direct hint
uses the five-minute fallback.

Phase 7 launch and status output must expose machine-readable fields, not require
the agent or tests to scrape prose. Launch output should include:

- `status`;
- `job_created`;
- `refusal_code`, when refused;
- `job_role`;
- `job_role_defaulted`;
- `poll_interval_sec`;
- `next_poll_after`;
- `cadence_basis`.

`cadence_basis` is a structured object. It should include, when applicable:

- `source`: one of `smoke_job`, `cadence_hint`,
  `conservative_no_evidence`, or `default`;
- `conservative_reason`, currently `missing_cadence_hint` when a fallback is
  selected;
- `smoke_job_id`;
- `smoke_lifecycle`;
- `smoke_runtime_sec`;
- `positive_viability_evidence`;
- `cadence_evidence_used`;
- `smoke_cadence_representative`;
- `expected_duration_sec`;
- `expected_duration_is_descriptive`;
- `cadence_hint_sec`;
- `skip_reason_recorded`;
- `selected_interval_sec`.

Human-readable guidance may accompany these fields, but tests and agent behavior
should rely on the structured fields.

Phase 7 polling state remains repo-local. Shared reservation
metadata may contain `last_heartbeat_at` and `heartbeat_interval_sec`, plus the
existing sanitized reservation identity fields. `job_role`, `smoke_*`,
`expected_duration_sec`, `cadence_hint_sec`, `smoke_skip_reason`, observed
runtime, `poll_interval_sec`, `cadence_basis`, `next_poll_after`, and
current-repo output pointers
belong in repo-local job state or current-repo tool output, not shared
cross-repo metadata.

During polling, the agent may discover the initial cadence is wrong. Phase 7
therefore adds an owner-side lifecycle action:
`manage_gpu_job(action="update_cadence", job_id=..., expected_duration_sec=...,
cadence_hint_sec=..., reason=...)`. A direct `cadence_hint_sec` is required and
used exactly. An accompanying `expected_duration_sec` is stored descriptively.
The server updates repo-local `poll_interval_sec`/`next_poll_after` and performs
a fresh heartbeat write without changing the shared lease interval. This action
records the agent's revised cadence judgment; it does not infer progress from
arbitrary output.

`update_cadence` is an owner-side lease mutation. It requires the current
`server_instance_id` to own the reservation, a fresh approved policy, a healthy
heartbeat manager, a non-empty `reason`, and `cadence_hint_sec`. It uses
the same serialized owner metadata update path and cleanup finalization guard as
heartbeat writes. It must not renew, adopt, or otherwise mutate a reservation
owned by another server instance.

When `manage_gpu_job(status)` observes that a smoke job has succeeded, status
output should make the smoke-to-main transition explicit. It should preserve the
smoke `job_id`, report the observed runtime when available, and guide the agent
to launch the main job with `job_role="main"` and
`smoke_job_id="<smoke job id>"` if the agent judges the smoke result useful. If
the agent uses the observed runtime or an expected duration to choose a polling
schedule, it should express that decision directly with `cadence_hint_sec`;
otherwise the main launch accepts the one-hour fallback. This keeps smoke evidence discoverable
through durable job state instead of relying on transient chat memory.

Smoke status output should include a structured `recommended_next_call` object
when a next action is useful. For a successful smoke job, that object can name
`run_python_on_gpu`, include `job_role="main"` and the `smoke_job_id`, and mark
`smoke_cadence_representative` as an optional agent judgment rather than a server
assertion.

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
`manage_gpu_job(status)` when appropriate. If status still reports a running
job, already-durable intermediate artifacts may be analyzed read-only and must
be labeled provisional. The hook then
records in its advisory state that this due timestamp was reminded, so normal
tool use is not spammed with the same reminder.

ADR 0004q amends this simple de-duplication rule with throttled re-reminders
until status acknowledgement. Phase 7 uses the ADR 0004q acknowledgement and
re-reminder behavior; it must not regress to permanent suppression for one due
timestamp.

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
has neither positive smoke viability evidence nor `smoke_skip_reason`, the hook
should inject a structured precondition: the agent should launch a representative
`job_role="smoke"` job first, or retry the main launch with a concrete
`smoke_skip_reason`. When a running job is due, the hook may remind the agent to
status-check the job and update cadence if observed progress contradicts the old
cadence.

The hook may also inject an early-poll warning, but only for an unambiguous
current-repo `manage_gpu_job(action="status", job_id=...)` call before that
job's `next_poll_after` and without `early_poll_reason`. The warning must be
advisory and non-blocking: it tells the agent the job is not due, suggests
independent work, and explains that an immediate full check requires
`early_poll_reason`. It must not warn for `check_gpus`,
`list_gpu_reservations`, unrelated tools, cross-repo jobs, malformed or
ambiguous targets, due jobs, terminal jobs, lifecycle actions, status calls that
already include `early_poll_reason`, or `PostToolUse`.

The Phase 7 cadence/preflight hook branch runs only after stale-policy handling,
fails quiet, and is advisory. It must not veto tool calls, mutate or synthesize
tool input, act as a scheduler, or write heartbeats or cadence state. The launch
tool's soft refusal and the status tool's compact `not_due_yet` response are the
server-side workflow guards.

The Phase 7 hook context should be compact and shaped like:

```text
GPU MCP: this main GPU launch has no successful smoke evidence.
Before launching, run a bounded representative
run_python_on_gpu(job_role="smoke", ...) with small inputs, max steps, dry run,
or a short smoke script; provide cadence_hint_sec when a non-default polling
schedule is useful. Otherwise retry the main launch with smoke_skip_reason explaining why
smoke is skipped.
expected_duration_sec is descriptive only. Cadence hints affect polling only;
neither replaces smoke or a skip reason.
```

The early-poll hook context should be compact and shaped like:

```text
GPU MCP: job job-20260530T123456Z-a is not due for status until 2026-05-30T12:45:00Z.
Do independent work until then unless this job's output is blocking or the user asked for an immediate check.
If a full check is justified now, call manage_gpu_job(action="status", job_id="job-20260530T123456Z-a", early_poll_reason="...").
```

## 8. Failure Boundaries

Heartbeat failure and late polling have different meanings.

If the owner cannot maintain a task heartbeat, including because shared metadata writes are failing, it cannot safely perform owner-side lifecycle mutations such as launch, retry, stop, or finish. Read-only status and explicitly fingerprint-gated rescue actions follow ADR 0004's rules.

If the agent misses a suggested check but the owner's heartbeat is still fresh, that is not a lease failure. The next status call should simply report the current state and provide updated guidance.

If the task heartbeat is stale, observers may inspect the remote process. They still must not clean up the reservation unless process proof shows the original job is gone or has a different identity.

## 9. Implementation Boundaries

The direct-hint and role-fallback contract is fixed above. The following remain
separate implementation concerns:

- heartbeat lifecycle and health states beyond the initial `healthy`/`unhealthy` boundary;
- the exact `PreToolUse` hook output shape and fallback behavior for Codex versions without `additionalContext`;
- the exact atomic file-write mechanics for hook-owned advisory reminder state;
- the exact cleanup policy for stale hook-owned advisory files after the shared reservation is gone;
- whether app-server scheduled turns are supported as an optional enhancement;
- tests for per-task cadence, stale detection, missed task checks, hook reminder delivery, and heartbeat writer failure.

## 10. Rejected Directions

### Ad Hoc Agent Memory As Polling State

Rejected. Polling cannot be an implicit instruction like "remember to check
later." `poll_interval_sec` and `next_poll_after` are explicit per-task state. If
the owner dies, the independent heartbeat stops and ADR 0004's
stale-plus-process-proof rule takes over.

### Per-Agent Heartbeat Only

Rejected. One agent may manage multiple GPU tasks with different expected
runtimes. Lease ownership and polling state belong to each task, not the agent
process as a whole.

### Fixed Polling Interval

Rejected. A single polling interval either wastes tokens on long jobs or reacts too slowly to short probes.

### Smoke Template Files

Rejected. Smoke arguments are workload judgment, not MCP policy. A repo-local
template or recipe file would add a second configuration surface, create path
and argument-substitution rules, and still not prove representativeness. Phase 7
uses normal managed GPU jobs for smoke tests and durable job records for smoke
evidence.

### Runtime Estimate as Cleanup Authority

Rejected. Estimates may guide the agent's explicit polling choice, but they do
not alter heartbeat cadence and cannot prove a GPU is free. Cleanup authority
remains the ADR 0004 two-condition rule.
