# ADR 0008: Shared GPU MCP Server and Quiet Codex Monitoring

## Status

Accepted design, 2026-10-06. The shared backend, ownership boundary, quiet
monitoring and terminal viewer are implemented. The selected human entry uses
Codex's native external-editor keyboard action, with one-time installer setup.
Users keep ordinary `codex` and `codex resume`. An exact plugin `/monitor`
command remains unresolved; the tested frontend wrapper is a prototype.
Implementation and compatibility evidence are recorded separately from the initial probe.
Amended on 2026-10-09: normal polling results reach the owner privately, pending
idle updates are combined, and transient verification has a ten-second alert grace.

An isolated live probe against Codex CLI and app-server 0.160.1 demonstrated
shared HTTP MCP access, native caller identity, private model context, and
session-specific job access. It did not implement production GPU MCP changes,
automatic backend startup, or a native `/monitor` view. The current
[test guide](../test/CODEX_SHARED_MONITORING.md) describes reproducible coverage;
individual probe reports remain local.

This decision applies to the Codex integration. Leave `claude-version/`
unchanged until the corresponding mechanisms have been verified for Claude.

## Context

Multiple Codex conversations can work in the same repository. Today each MCP
server instance owns the jobs it launched, while the job-reminder hooks scan
the repository's managed jobs. The hooks do not associate those jobs with the
conversation that launched them. A session can consequently receive another
session's reminder, inspect its job, advance its polling time, or consume shared
reminder-throttle state.

The reservation protocol already prevents concurrent acquisition of the same
GPU by instances using the same registry and canonical GPU key. Acquisition
atomically creates a reservation directory. Sharing a server simplifies job
ownership and monitoring; it is not the first protection against double booking.
The registry remains necessary because different repositories can request the
same physical GPU.

The current reminder path also creates unnecessary work in the conversation:
the hook announces that a check is due, the agent calls `manage_gpu_job`, and a
normal result says the job is still running. Shorter tool returns reduce text
but retain visible tool activity and model work. Human inspection should be
available separately from this conversation.

## Decision

Use one job-owning GPU MCP backend per repository through the Codex app-server
integration. Keep each session's jobs private to that session. Perform routine
checks in backend code, deliver useful events to the originating session, and
provide an optional human monitor over the same saved job information.

Routing is ordinary code using recorded caller identity. There is no dedicated
repo agent for routing, polling, or keeping the backend alive. Scientific
interpretation remains with the task agent.

```text
Codex session A -- connection A --\
                                 one GPU MCP backend for the repo --> GPU jobs
Codex session B -- connection B --/                  |
                                                   +--> saved job state and logs
                                                   |          |
                                                   |          +--> human monitor
                                                   |
                                                   +--> owner event --> app-server
                                                                        |
                                                         originating Codex session
```

### Automatic startup and shared lifetime

Opening or resuming a configured Codex session must start or connect to the
repository's backend automatically. Users must not start an HTTP server or
open a monitor before using GPU MCP.

A configured MCP startup launcher can ensure that the backend exists, then
connect the session to it. Concurrent startups must serialize backend creation
and reuse the same healthy instance. Identify the repo by its canonical policy
root, including sessions opened from subdirectories.

Connection processes may be session-specific and stateless. They must forward
caller metadata and must not become separate owners of jobs or reservations.
Only the shared backend owns lifecycle state and the heartbeat manager. Closing
a connection or the monitor must not stop an active job or its monitoring.

The probe demonstrated a shared HTTP process started by its test harness.
Ordinary stdio configuration still started separate processes. Stock Codex
0.160.1 has not been shown to supervise one GPU MCP process per repo by itself.
The launcher is the initial integration path; native daemon process sharing is
a further Codex integration choice. HTTP is an internal transport, with local
access controlled by the integration.

### Codex daemon compatibility and upgrades

Amended on 2026-10-08 after the pin caused ordinary Codex upgrades to require
manual daemon repair. The earlier exact-version lock is retired. `--from-cli`
selects a fixed package and disables production updates; it does not follow
later CLI upgrades.

Codex owns its global daemon installation and update channel. GPU MCP checks
that a daemon is reachable, then uses native initialization and RPC responses
to establish compatibility. Different version numbers alone do not block a
connection. Unsupported or failed RPCs remain explicit connection/delivery
failures; durable events keep their normal recovery rules.

GPU MCP must not pin, update or restart the global daemon, alter shared feature
flags, or make normal Codex use in another repository depend on MCP installation.
Use Codex's native production updater for daemon maintenance. Keep one canonical
CLI installation so shell lookup does not alternate between old standalone and
new npm executables. Feature compatibility and authentication remain separate.

### Session ownership and access

Capture the calling Codex `threadId` at launch from the integration's request
metadata. Record it as the job's originating thread in service-owned persistent
state. The probe also observed `sessionId` and `windowId`; those fields are
useful context but do not replace the actual thread identity for authorization.

Every agent-facing job operation must enforce the same origin check before
returning information or changing state. This includes listing, status, logs,
artifacts, progress, smoke evidence, stop, retry, finish, and timing updates.
Knowing another job's ID must not grant inspection. A new or forked conversation
does not acquire another thread's jobs merely by sharing its repo or history.
Resuming the same originating thread retains its association.

Agent-visible resource reports may indicate that a GPU is unavailable without
disclosing another session's job IDs, process details, logs, or output paths.
Apply this boundary to all tools that could reveal managed-job information,
not only `manage_gpu_job`. Policy recovery remains subject to its existing
approval rules.

Origin identity must come from the trusted Codex integration, not a
model-supplied tool argument or an unrestricted HTTP client's assertion.
Worker-writable records and display mirrors are not authoritative ownership
records. Missing identity fails closed in the Codex integration. Jobs whose
origin is unknown during migration are not assigned to whichever session
happens to inspect the repo next.

The backend owns reservations on behalf of all its jobs; session ownership
restricts which agent can access each job. Backend process ownership and
conversation ownership remain separate checks. A backend restart does not
automatically adopt or renew reservations created by a previous server instance.
It does reconcile verified GPU-job completions from that instance. Save the result and
its owner event first, then recover a matching stale reservation only after
process-gone proof and a locked reread of the job, attempt, process identity,
owner and heartbeat. A missing or replaced reservation is recorded as inactive
for the old job. Alive or uncertain processes retain their reservations and
require explicit recovery; the new backend gains no stop or retry authority.

This boundary covers GPU MCP interfaces and delivered context. Shared workspace
files and out-of-band access to the local daemon are separate trust boundaries;
this ADR does not establish operating-system isolation between conversations.

### Fixed heartbeat timing

A heartbeat updates `last_heartbeat_at` in the reservation's metadata file. It
is an ownership check-in performed by backend code. It consumes no model turn
and performs no remote job inspection.

Use these fixed rules for the Codex backend:

- Update each owned GPU reservation's timestamp roughly every 60 seconds.
- Consider it stale when the last successful update is more than 600 seconds
  old: ten minutes without a check-in.
- Expose no agent-selected heartbeat interval or stale timeout.

This changes the actual stale threshold from the current default of thirty
minutes to ten minutes. Changing a ten-minute metadata field while retaining
the current three-times multiplier would not implement this decision. New
stale decisions use the fixed 600-second threshold; any retained legacy
interval field is compatibility/reporting data, not a timing override.

Staleness permits investigation, not automatic release or termination. Cleanup
still requires proof that the original process is gone, followed by the existing
locked reread of reservation identity and timestamp. An alive process or an
unknown inspection result keeps the GPU reserved. Ordinary verified completion
can release a reservation earlier without waiting for the stale threshold.

GPU-assisted builds use the GPU lease. CPU-only builds have no GPU reservation
or GPU heartbeat, as required by [ADR 0007](0007-separate-build-and-execution.md).

### Backend checks and concise tools

Polling means checking how a job is doing. The shared backend performs scheduled
process checks and notices durable outcomes. Reuse the existing validation and
lifecycle code; do not add another job scheduler or scientific workflow engine.
Keep this work separate from the heartbeat's timestamp-writing loop.

The agent no longer needs a recurring reminder to call `manage_gpu_job` just
because a healthy job's next checking time arrived. Backend checks advance
the saved checking schedule and save a short polling update for the owner.
The checking time schedules backend work, not an agent-owned timer. The existing role defaults can remain starting
points: five minutes for smoke jobs, one hour for main/one-off jobs, and thirty
seconds for builds. Outcome-file detection can notice completion sooner.

Agents may still request a different checking interval or a temporary milestone
notification. Existing `cadence_hint_sec` compatibility can express the former;
it changes job-check timing, not heartbeat timing. Runtime estimates remain
descriptive. Routine launch must not require the model to invent a checking
interval every time.

Default tool responses identify the requested job, its current result, and a
useful output pointer. Detailed process identity, reservation diagnostics,
logs, and progress history remain available through explicit inspection of
that session's jobs. A status such as “running normally” must describe the
observed process and known errors, with its check time; it does not certify
scientific progress or numerical correctness.

Stable tool descriptions explain automatic checks, private results and explicit
inspection. Returns and events carry current facts. Waiting for completion alone
needs no repeated status/list calls; a concrete question or decision remains a
valid reason to inspect. Completion supplies task state without requiring an
acknowledgement tool call, `finish` call or immediate follow-up work.
Compact Codex replies use `next_backend_check_at`; full diagnostics retain the
existing `next_poll_after` field and interval controls.

Retry temporary verification failures in backend code after two seconds during
an initial ten-second grace. Persistent identity failures retry more slowly;
late result reads continue independently of the normal polling interval. Keep
uncertainty visible in the monitor immediately, but allow recovery before waking
the agent with an uncertainty alert. Actual job failures are not delayed. Grace
expiry does not prove success, failure or permission to release a GPU.

### Private events and visible alerts

The job's recorded originating thread determines the recipient. Short polling
results enter private model context while the owner is active. Keep only the
latest pending update per job while idle or unloaded; do not wake or resume an
idle owner just to acknowledge normal health. Completion and problems use the
existing queued wake-up path. Failures and abnormal conditions
can produce concise visible alerts in the originating conversation.

| Observation | Task agent | Human conversation | Monitor |
|---|---|---|---|
| Successful lease check-in | No message | No message | Updated ownership information |
| Normal scheduled process check | Short private result when active; latest pending update while idle | No routine commentary | Updated status and check time |
| Verified completion | Private result; wake when continuation is needed | No routine completion announcement | Completed state and outputs |
| Requested milestone | Concise private event | Visible only when requested | Progress/event update |
| Job failure | Concise result with diagnostic pointer | Visible alert | Failure and logs |
| Monitoring, lease, or delivery problem | Scoped actionable information | Throttled alert when attention is needed | Explicit uncertainty or pending delivery |

Do not make a successful event another instruction to call status merely to
retrieve the result that the backend already reconciled. Preserve
[ADR 0006](0006-agent-guidance-boundaries.md)'s distinction between machine facts
and scientific judgment. Published intermediate artifacts remain provisional
until a verified terminal result establishes completion.

Normal polling uses `thread/inject_items` to append private data without starting
a new user turn. Completion/problems use queued `turn/start` with `toolOutput`
and application context. Native tests check hidden polling data, owner recall,
foreign-session exclusion and continuation of a neutral build task.

The native UI still shows working/completion activity when a model turn starts.
The model can also write ordinary commentary. A silent success reply was
observed, not guaranteed for every prompt or model. Hidden context still uses
tokens. Combine unsent healthy updates while idle. Hiding all activity or all model-written text would require frontend
enforcement beyond the demonstrated private payload delivery.

The hook `suppressOutput` field is not an implemented hiding mechanism in the
[current hook documentation](https://learn.chatgpt.com/docs/hooks). Background
hooks remain a possible delivery adapter, but were not live-tested here.

### Completion persistence and delivery

Amend [ADR 0005](0005-automatic-outcome-reconciliation.md)'s coupling of an
agent-issued status call, result delivery, and GPU release. The owning backend
may run the same status reconciliation internally. For every managed job,
including CPU-only builds, persist the terminal result and pending owner event
as one recoverable transition before retiring its checking schedule and before
releasing any GPU reservation. Use an atomic update or deterministic recovery
that repairs an interrupted result/event transition. If checking ends with an
unknown outcome, persist that uncertainty and its owner event rather than
inventing a terminal result. Delivery failure must not erase the result or send
it to another live session.

Serialize background reconciliation with agent-requested status, stop, retry,
and finish per job, or use equivalent conditional updates against the current
attempt and state. An observation from an older attempt must not overwrite a
new attempt or announce itself as that attempt's current result. Event
deduplication alone does not protect these lifecycle transitions.

Keep notification state with the existing managed-job state. Use stable event
identity based on job, attempt, and transition or requested milestone; deduplicate
across hooks, retries, and reconnects. Polling also keys by the check time;
separate uncertainty episodes key by their original verification-pending time.
Record acceptance by the Codex delivery
interface separately from the model acting on the event. Persisted history
acceptance is not proof that an agent performed the next scientific step.

Before retiring the Stop wait, verify delivery during an active turn, an idle
turn, and concurrent user input. A separate “check idle, then start a turn”
sequence is still subject to the race identified in ADR 0005. Select an atomic
queued operation, or establish safe retry behavior without interrupting or
duplicating the user's turn. The idle-wake probe alone does not establish this.

Unloaded or disconnected originating threads retain their pending events.
Archive/deletion handling must respect the user's thread lifecycle; it must
not silently redirect events or unarchive a deliberately archived task.
Delivery problems remain visible in the human monitor.

### Human monitor and Scientific Workflow

The `/monitor` target is an optional terminal dashboard that temporarily opens
over the conversation and returns on Escape. It can be invoked from a new
session in the repo and display repo-wide information for the human. Its data
comes through a human UI interface, not an agent-exposed repo-wide job tool,
and must not be inserted into that session's model context.

Start with a read-only view of running jobs and recent outcomes. Show job,
originating session label, script/job kind, host and GPU or CPU-only mode,
status, elapsed time, last check, and progress when explicitly reported.
Details and logs open on selection. Preserve unknown progress and uncertain
inspection results; do not infer scientific progress from GPU utilization.
Reading the view must not consume the originating agent's notifications or
advance its checking schedule.

Reuse a small portion of
[Scientific Workflow](https://github.com/dingyisun0101/Scientific-Workflow): task
table rendering, status colors, progress display, messages, and keyboard
navigation. A small adapter maps GPU MCP snapshots and events into that view.
Its [current UI contract](https://github.com/dingyisun0101/Scientific-Workflow/blob/main/rust/src/ui/api.md)
connects presentation to its runtime through private interfaces. Extract or
expose a display boundary rather than importing the whole execution engine.
Closing or failing to render our viewer must not cancel GPU jobs or make backend
execution require an interactive terminal. Later, opt-in reporting helpers can
supply structured stage/step events from scientific jobs.

A plugin can package startup integration and the viewer. A supported plugin API
for custom terminal popups has not been established. The user entry point must
remain ordinary `codex` and `codex resume`, including when the popup is available.

The selected entry is Codex's existing Ctrl+G terminal handoff. A small human
helper offers the viewer or normal prompt editing, and leaves the editor draft
unchanged after monitoring. The installer owns shell setup, preserving the
previous editor and applying the setting automatically in new terminals. No
daemon restart, skill prompt or model decision is needed to open the view.
Keyboard acceptance must cover draft preservation, private display data,
selection/Escape and operation while another task is active.

A small PTY wrapper was tested as a prototype: typed `/monitor` opens the human
viewer, and Escape restores the conversation. Native tests verified human-only
canary data, unchanged turn count and return to the original conversation. This
prototype is not the selected product integration and is not required for GPU MCP
or the human two-session core-behavior experiment. Native plugin UI acceptance
remains open.

## Relationship to Existing Decisions

| Existing decision | Effect of this ADR |
|---|---|
| ADR 0004 | Retain atomic claims, shared registry, process identity, and cleanup guards. |
| ADR 0004p | Retain independent lease and job-check timing; fix the stale threshold at ten minutes for the Codex backend. |
| ADR 0004q and ADR 0004r | Replace repo-wide agent polling reminders in the new Codex path with origin-scoped backend events and corresponding tests. |
| ADR 0005 | Permit internal status reconciliation with durable owner events before release; verify queued delivery before retiring Stop. |
| ADR 0006 | Keep terse facts and scientific judgment separate; provide diagnostics on request. |
| ADR 0007 | Preserve build/execution separation, authoritative build records, resource modes, and process-tree proof. |

Existing policy approval, reload, path, guard, and process-identity gates remain
in force. Lease expiry, lost context, or a closed monitor never grants permission
to release an unproven GPU or retry over a live process.

## Implementation and Acceptance

Implement incrementally within the Codex path:

1. Capture authoritative origin identity and enforce it on every agent-facing
   read and mutation. Establish explicit handling of legacy jobs with no origin.
2. Add automatic shared-backend startup/reconnect and internal scheduled checks,
   retaining the reservation protocol and existing lifecycle implementations.
3. Persist owner events and integrate private delivery, concise diagnostics, and
   visible failure alerts. Retire redundant polling reminders only after the
   active/idle/recovery cases pass.
4. Adapt the small Scientific Workflow display portion and verify its launch
   integration. Keep Claude work separate.

Required acceptance evidence includes:

- Two simultaneous sessions in one repo automatically reach one job-owning
  backend; different repos retain their policies and coordinate GPU claims
  through the shared registry. Users perform no manual server startup.
- Own-job operations succeed; foreign-job operations reveal no job details,
  including ID guesses, listings, smoke references, process queries, retries,
  log/artifact reads, and forged caller fields. Verify new, resumed, forked,
  and delegated threads rather than assuming their IDs are interchangeable.
- Opening/closing sessions and the monitor does not lose active leases or
  notification state. Backend death, restart, policy drift, missing identity,
  and unsupported daemon versions produce bounded, actionable behavior.
- Ten minutes of heartbeat silence permits investigation; alive and unknown
  processes remain reserved. Cleanup and heartbeat races retain the final
  locked reread. Test legacy metadata and mixed-version readers explicitly.
- Real GPU jobs and both build modes are checked without repeated agent-issued
  status calls. Healthy steady state has no model wakeups or routine transcript
  messages. Completion still reaches the correct task promptly.
- Delivery survives reconnects, duplicates, active turns, simultaneous user
  input, and delayed shared-filesystem outcome visibility. No broad broadcast
  or unsafe check-idle/start sequence is used.
- Interruptions between result persistence, event persistence, checking-schedule
  retirement, and GPU release recover without losing the owner event. Include
  CPU-only builds, where no GPU release operation triggers this protection,
  and unknown-outcome paths. Concurrent retry/reconciliation cannot overwrite a
  new attempt or deliver an old observation as its current result.
- Native terminal captures distinguish private event payloads, tool activity,
  model commentary, and visible failure alerts. Repo-wide monitor data never
  enters a session's model context.
- `/monitor` works without starting jobs, becoming a second controller, or
  making a human viewer a prerequisite for backend execution.

## Alternatives

- A dedicated repo agent for routing would add model work and another task to
  supervise. Recorded caller identity and backend rules already determine the
  recipient and ordinary notification behavior.
- Shortening every agent-issued polling response leaves visible tool activity
  and repeated model work. Use backend checks and event delivery instead.
- Sharing the backend while exposing every repo job to every agent violates
  session isolation. Reserve repo-wide inspection for the human viewer.
- Adopting the full Scientific Workflow runtime would replace substantially
  more execution infrastructure than this monitoring interface requires.
- Requiring manual HTTP startup would violate the normal session-opening flow.

## References

- [Codex app-server protocol](https://learn.chatgpt.com/docs/app-server).
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).
- [Codex hooks](https://learn.chatgpt.com/docs/hooks).
- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins).
- [Scientific Workflow UI contract](https://github.com/dingyisun0101/Scientific-Workflow/blob/main/rust/src/ui/api.md).
- [Shared monitoring test guide](../test/CODEX_SHARED_MONITORING.md).
