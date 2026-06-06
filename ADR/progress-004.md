# ADR 0004 / 0004p Battlefield-Driven Implementation Progress

Status: planning draft.

This file records the implementation route for ADR 0004 multi-agent GPU
coordination and ADR 0004p per-task heartbeats.

The implementation must be battlefield-test driven. The point is not to build a
set of tidy internal modules and then hope they compose. The point is to prove,
slice by slice, that agent-visible multi-repo behavior works: two repo agents
sharing one GPU namespace must not double-book, must not free live work, must
recover lost context, and must guide the agent without becoming a daemon or
scheduler.

The phase notes below are not a promise that the first implementation attempt is
correct. They are invariants, pressure points, and likely seams to keep in view
while the acceptance tests force the design into shape. If a battlefield test
fails in a way that contradicts the expected code shape, the code shape changes;
the invariant does not.

## Test Strategy

Every major phase starts with an agentic battlefield scenario written from the
outside. The default battlefield is simulated-cluster, not live-cluster: Codex is
real, the MCP tool surface is real, and the coordination state machine is real,
while the GPU/process/time/failure facts are injected by the harness.

In this document, **battlefield** means Codex is actually in the loop, using the
MCP tools the way an agent would use them. The main suite is:

- **`codex exec` battlefield checks** are the repeatable form. A prompt is run in
  a test repo through `codex exec`, with MCP tools available. Repo A and Repo B
  share an injected reservation registry and simulated cluster backend. This is
  where double-booking, stale cleanup, ownership, heartbeat health, recovery,
  retry, finish, and hook guidance are proved.

Deterministic pytest/unit/integration tests are supporting tests, not the
agentic proof. They provide fake repos, fake SSH, fake `nvidia-smi`, fake clocks,
barrier-controlled races, fixture process tables, and filesystem fault injection
so sharp edge cases are cheap to regress.

Live GPU behavior is infrastructure validation, not the regression backbone.
Adapter fidelity is tested from captured real-host fixtures for `nvidia-smi`,
`ps`, SSH stdout/stderr, process inspection, and launcher outcome records. A
real host is validated through the host-onboarding procedure below before it is
trusted by policy. No implementation phase depends on a live GPU probe to prove
the coordination design.

Every battlefield item should be readable without knowing the implementation. It
should state the situation, what the agents or tools try to do, and the required
outcome.

Definition of done for every slice:

1. Write the simulated-cluster `codex exec` battlefield scenario first and state
   the expected failure before implementation.
2. Add the deterministic supporting tests and harness seams needed to make that
   scenario reliable: fake repos, simulated cluster state, captured adapter
   fixtures, fake clocks, and fault injection where needed.
3. Add only enough production code and supporting tests to pass the scenario.
   Expect the first implementation to change after the agentic run exposes wrong
   tool output, missing guidance, bad timing, or an awkward boundary.
4. Run the `codex exec` battlefield check. A phase is not complete until its
   agentic battlefield check passes. If adapter-facing code changed, add or
   update fixture-based adapter tests.
5. Re-run the supporting tests and relevant existing regression tests.

## Host Onboarding Validation

Host onboarding is a manual infrastructure procedure, not a regression test and
not a phase gate. It validates that one real GPU host obeys the adapter
assumptions used by the simulated cluster.

Run it when adding a new host, changing the SSH launch strategy, or changing the
adapter facts collected from real hosts:

- [ ] Confirm the active policy can reach the host over SSH and that approved
  script roots, output roots, Python command, and GPU indices match the policy.
- [ ] Capture or refresh fixture samples from the host for `nvidia-smi`, `ps`,
  process-inspection output, SSH launch stdout/stderr, and launcher outcome
  records. Parser tests must pass on those fixtures before the host is treated
  as supported.
- [ ] Launch `quick_success.py` through the MCP tool surface in a test repo.
  Verify the reservation records the expected host/GPU, `job_id`, remote PID,
  non-sensitive process fingerprint or nonce, heartbeat timestamp, and
  repo-local output/log pointer. Shared metadata must remain sanitized. After the
  fixture exits, verify status reports the expected output marker/outcome and
  finish removes the reservation.
- [ ] Launch `term_delay.py` or `hold_gpu.py` through the MCP tool surface in a
  test repo. Verify `check_gpus` and `manage_gpu_job(status)` report the live
  fixture consistently with the reservation facts. Stop it through the MCP tool
  surface, verify the process is gone, then finish/cleanup the reservation.
  Harness cleanup must still confirm fixture identity before sending any fallback
  signal.

Passing host onboarding means the host is suitable for the simulated suite's
adapter assumptions. It does not replace simulated-cluster battlefield tests,
race tests, malformed-state tests, or failure-injection tests.

## Current Code Gaps

- `check_gpus` reads `nvidia-smi` only; it does not consult a reservation
  registry.
- `run_python_on_gpu` still has direct sync and async launch paths; it does not
  reserve before launch or return a managed `job_id`.
- `kill_gpu_process` has the closest process-inspection logic, but that logic is
  not yet shared by launch, status, cleanup, retry, finish, and kill.
- `gpu_mcp_policy_hook.py` now includes stale-policy workflow feedback and the
  Phase 6 reminder branch; keep future edits scoped to those two hook
  responsibilities.
- Earlier phases added the shared per-user reservation store, repo-local managed
  job store, heartbeat manager, `manage_gpu_job`, and
  `list_gpu_reservations`; remaining work should treat those as implemented
  surfaces rather than greenfield gaps.

## Non-Negotiable Review Constraints

These constraints come from ADR 0004, ADR 0004p, and the recent review passes.
They should be treated as regression risks throughout implementation.

- Shared metadata is minimal and sanitized. Do not store args, full script paths,
  or output paths in shared cross-repo metadata.
- Do not persist a `state` field. Compute reservation state on demand.
- Never free a reservation from heartbeat staleness alone.
- Cleanup never transfers ownership; new ownership requires a fresh successful
  atomic `mkdir`.
- A new MCP server instance may inspect and clean stale-gone reservations, but
  must not adopt or heartbeat reservations owned by an old `server_instance_id`.
- Owner-side metadata writes must go through one reservation update path. Atomic
  replace prevents torn files, but it does not by itself prevent lost updates
  between heartbeat and lifecycle handlers.
- `process_fingerprint` or launcher nonce must not include command-line args.
- Zombies count as process-gone for cleanup/status purposes.
- A stale reservation with null `remote_pid` and unknown launch outcome fails
  closed unless there is explicit proof no remote process was launched.
- `async_mode=False` is compatibility input only. It returns a managed job
  handle, not inline blocking stdout/stderr completion.
- Hook reminders are best-effort behavior, not the safety boundary.
- Rescue kill is explicit, fingerprint-gated, and does not remove reservations.
  It is not human-approval-only; an autonomous agent may choose it for a specific
  inspected target.
- All operational tools obey ADR 0002 active-policy freshness. If `gpu-mcp.toml`
  changed but has not been approved and reloaded, reservation-aware tools refuse
  normal operation rather than bypassing policy.

## Phase 0: Simulated Battlefield Harness And Fixed Contracts

This phase is not a design step. The values below are fixed by ADR 0004 and ADR
0004p. The work is to make them executable and testable before behavior slices
depend on them.

Agentic battlefield smoke checks:

- [ ] A `codex exec` run can be launched inside Repo A, and another can be
  launched inside Repo B. Both agents use MCP tools, not internal Python helpers,
  shell commands, SSH, or direct registry edits.
- [ ] Repo A and Repo B point at the same fake reservation registry. When Repo A's
  agent creates a reservation through the tool surface, Repo B's agent can see
  it, while each repo still has its own private job state.
- [ ] A `codex exec` run calls launch, status, and check tools from the outside
  and gets the fields a user-facing agent needs.

Public test contracts:

These contracts are the public surface for the agentic scenarios and the test
suite. They are derived from ADR 0004's tool behavior, the ADR 0004
implementation companion, and ADR 0004p's heartbeat/status guidance. They are
not internal module APIs.

The agent-facing MCP tools are the only way a battlefield agent may exercise GPU
coordination behavior:

- [ ] Reservation-aware tools must expose JSON-compatible structured fields for
  the contract below. They may also include human-readable text, but tests and
  agent guidance must not depend on scraping prose.
- [ ] `check_gpus(...)` is the read/report surface for GPU availability. Its
  result must let the agent and tests identify each GPU by `host` and
  `gpu_index`, distinguish available from reserved/not-safely-available, and see
  reservation details when present: `reservation_key`, computed reservation
  state or conservative reserved status, heartbeat age/staleness, owning repo or
  sanitized job label when available, and inspection summary when performed. If
  the registry cannot be read, it must not report GPUs as available based only on
  `nvidia-smi`. In that case the result must include top-level
  `registry_status: "unavailable"` and sanitized `registry_error` fields, and
  every GPU row must be reported as
  `availability: "unknown_unavailable"` rather than `available`.
  Fresh-heartbeat reservations that have not been inspected must be reported as
  conservatively reserved, such as `reservation_state: "RESERVED"`. They must
  not be reported as `RESERVED_IDLE` unless process inspection has actually
  proved the stored process is gone.
- [ ] `run_python_on_gpu(host, gpu_index, script_path, args=None,
  async_mode=False, output_file=None)` is the launch surface. Both
  `async_mode=True` and `async_mode=False` return a managed job handle rather
  than blocking for job completion. A successful launch result must include
  `status`, `job_id`, `reservation_key`, `host`, `gpu_index`,
  `server_instance_id`, process identity summary when known, current-repo
  output/log pointer when available, and `next_poll_after`. Phase 7 launch
  results also include `job_role`, `job_role_defaulted`,
  `heartbeat_interval_sec`, and structured `cadence_basis`. A refusal must say
  why the launch did not happen, include a structured refusal code when
  available, and must not create a job that the caller then has to manage.
- [ ] `manage_gpu_job(action, job_id=None, reservation_key=None,
  early_poll_reason=None)` is the
  status/lifecycle surface for `action="status"`, `"stop"`, `"retry"`, and
  `"finish"`, and in Phase 7 also `action="update_cadence"`. A status result
  normally includes `job_id`, `reservation_key`, computed reservation state, job
  lifecycle when known, whether this server owns the job, heartbeat
  age/staleness, process inspection summary when performed, allowed actions,
  current-repo output/log pointers when available, and next poll guidance.
  Phase 7 may return compact `polling_state="not_due_yet"` for early
  no-reason status calls; that compact response is not a full status check.
  Stop/retry/finish/update-cadence must return the same kind of handle or
  refusal context so the agent knows what remains safe to do.
- [ ] `list_gpu_reservations(scope="mine"|"all", fresh=False)` is the
  read/report and lost-context recovery surface. Candidate rows must be
  sanitized and include enough information for an agent to choose explicitly:
  `job_id`, `reservation_key`, `host`, `gpu_index`, `script_name`, computed
  state or conservative reserved status, heartbeat age, owning
  `server_instance_id`, whether the current server owns it, allowed actions, and
  last inspection summary when available. It must not expose args, full script
  paths, or output paths from shared metadata. `fresh` is not a filter. With
  `fresh=False`, listing is a fast filesystem read: compute heartbeat
  freshness from timestamps and include only cached or already-recorded
  inspection summaries. With `fresh=True`, listing may perform the same bounded
  stale-reservation inspection and stale-gone cleanup allowed for `check_gpus`;
  it still must not deep-inspect fresh reservations merely to refine their
  label.
- [ ] `kill_gpu_process(host, pid, fingerprint=None, signal="TERM")` is the
  rescue surface. It targets one inspected host/PID, requires fingerprint
  confirmation before signaling, and never removes a reservation. Its result must
  tell the agent whether it only inspected, signaled, or refused, and must leave
  cleanup to the normal reservation rules.

The test-harness control surface is public to tests but invisible to the agent:

- [ ] Create isolated Repo A / Repo B worktrees with separate repo-local job
  state and a shared injected reservation registry.
- [ ] Generate per-repo MCP configuration/environment so Repo A and Repo B share
  the intended injected registry and simulated cluster backend.
- [ ] Run one or two `codex exec` agents, including concurrent runs when needed,
  and capture each final answer plus the structured tool-visible results.
- [ ] Choose whether a scenario keeps one MCP server alive for multiple tool
  calls or intentionally starts a new server instance to model restart/death.
- [ ] Configure fake policy nodes, fake GPUs, fake process table facts, fake
  `nvidia-smi` facts, fake launch outcomes, and fake outcome records.
- [ ] Advance fake time so heartbeat freshness/staleness can be tested without
  sleeping.
- [ ] Force registry read failures, registry write failures, malformed metadata,
  host inspection failures, and client disconnect windows. Failure injection must
  be selective by server instance or repo when a scenario needs one server's
  writes to fail while another server's reads still work.
- [ ] Mutate fake process, host, registry, clock, and filesystem-failure state
  out of band while a `codex exec` scenario is running. The Codex agent must not
  call those controls directly.
- [ ] For agentic battlefield scenarios, out-of-band mutation happens only
  between tool calls: either between separate `codex exec` runs, or after an
  instrumented test MCP server records that a specific tool call has completed.
  Mid-tool mutation is reserved for supporting race/fault tests, not normal
  battlefield prompts.
- [ ] Run `codex exec` in a selected repo with only the intended MCP tools
  exposed, then assert on the tool-visible results and on sanitized registry/job
  state after the agent run.
- [ ] Use the installed global GPU MCP Codex companion hook for Phase 6 hook
  capability probes. The harness may seed repo-local job state and injected test
  environment, but the Codex agent must not edit hook config or hook state
  directly.

Fixture jobs, adapter fixtures, and process controls:

- [ ] Provide small fixture scripts for simulated-cluster tests and host
  onboarding validation:
  `hold_gpu.py` starts, writes a ready marker, lightly holds the assigned GPU or
  fake GPU slot, and exits on SIGTERM; `term_delay.py` handles SIGTERM but exits
  after a short delay; `ignore_term.py` ignores SIGTERM for rescue-kill tests;
  `quick_success.py` exits 0 after writing a known output marker; `quick_fail.py`
  exits nonzero after writing a known output marker; `off_gpu_phase.py` stays
  alive while not appearing on the GPU.
- [ ] Provide captured adapter fixtures from real hosts for `nvidia-smi`, `ps`,
  SSH launch stdout/stderr, process-inspection output, and launcher outcome
  records. Parser and adapter-shaping tests must use these fixtures; live GPU
  behavior is covered by host onboarding, not by phase tests.
- [ ] The harness can wait until a fixture job has definitely started and can
  record the launched PID, fingerprint, start time, output path, and reservation
  key for assertions and cleanup.
- [ ] The harness may send TERM/KILL over SSH only to fixture processes it
  launched and identified, after confirming fingerprint/start-time identity.
  Codex agents must not improvise SSH process management in battlefield tests.
- [ ] Every battlefield scenario that launches a real or fake process must have
  harness cleanup for leftover fixture processes and injected reservations.
  Cleanup must handle interrupted states such as "stopped process, reservation
  not yet finished" by using fixture identity checks and test-registry cleanup,
  not by relying on the Codex agent to repair the test.
- [ ] Zombie behavior is primarily a supporting fake-process-table test unless a
  special reliable zombie fixture is deliberately added later.

Invariants and implementation pressure:

- [ ] Materialize `DEFAULT_HEARTBEAT_INTERVAL_SEC = 600`.
- [ ] Materialize `MIN_HEARTBEAT_INTERVAL_SEC = 60` and
  `MAX_HEARTBEAT_INTERVAL_SEC = 3600`.
- [ ] Materialize `STALE_MULTIPLIER = 3`.
- [ ] Materialize the shared registry root constant:
  `~/gpu-mcp/state/reservations/`. Production uses ADR 0002 trusted state; tests
  inject an alternate root.
- [ ] Materialize the repo-local managed job state root constant:
  `<repo>/.gpu_mcp_state/jobs/`.
- [ ] Materialize the repo-local managed job layout:
  `<repo>/.gpu_mcp_state/jobs/<job_id>/job.json` for current-repo launch
  details and reminder state, and
  `<repo>/.gpu_mcp_state/jobs/<job_id>/attempts/<attempt_id>/outcome.json` for
  launcher terminal outcome. Shared reservation metadata must never contain the
  repo-local job path, full script path, args, or output path.
- [ ] Materialize the canonical reservation key helper:
  `<canonical-host>.gpu<gpu-index>`, where `canonical-host` is the active policy
  node name after host validation.
- [ ] Materialize the `job_id` generator:
  `job-<UTC timestamp>-<random suffix>`.
- [ ] Materialize the `attempt_id` generator:
  `attempt-<UTC timestamp>-<random suffix>`. Launch creates the first attempt;
  retry creates a new attempt under the same `job_id` and reservation.
- [ ] Materialize the `server_instance_id` generator:
  `server-<local-hostname>-<pid>-<random suffix>`.
  The random suffix is the uniqueness source; hostname and PID are diagnostic.
- [ ] Pin the repo-local launcher outcome record format. The outcome file is
  JSON with `schema_version`, `job_id`, `attempt_id`, `reservation_key`, `host`,
  `gpu_index`, `remote_pid` when known, `terminal_status` (`"success"`,
  `"failure"`, `"signaled"`, or `"launcher_error"`), `exit_code`, `signal`,
  `started_at` when known, `ended_at`, and optional bounded `error_summary`.
  Missing, unreadable, malformed, or mismatched outcome records map to
  process-gone-with-unknown-outcome.
- [ ] Pin launch result fields required by ADR 0004: `status`, `job_id`,
  `reservation_key`, `host`, `gpu_index`, `server_instance_id`, process identity
  summary when known, current-repo output/log pointer when available, and
  `next_poll_after`.
- [ ] Pin status result fields required by ADR 0004: `job_id`, `reservation_key`,
  computed reservation state, job lifecycle when known, ownership, heartbeat
  age/staleness details, process inspection summary when performed, allowed
  actions, current-repo output/log pointers when available, and next poll
  guidance.

Supporting tests:

- [ ] Constants and ID/key format tests.
- [ ] Launch/status response shape tests.
- [ ] Harness test proving two repo instances share the same injected registry
  but have separate repo-local job roots.
- [ ] Adapter fixture tests proving captured real `nvidia-smi`, `ps`, SSH, and
  launcher outcome samples are parsed into the same structured facts used by the
  simulated cluster.

## Phase 1: No Double Booking From Launch

User-visible requirement: two repo agents contending for one GPU must not both
launch onto it.

Agentic battlefield first:

- [ ] Repo A and Repo B are both instructed to target the same explicit
  `host`/`gpu_index` through `run_python_on_gpu`. Repo A launches `hold_gpu.py`
  first and gets a job handle. Repo B then tries the same GPU through the MCP
  tool and is refused because Repo A is already using it.
- [ ] After Repo A launches, Repo B runs `check_gpus` and sees that GPU as
  reserved, not available.
- [ ] Repo A and Repo B are both instructed to launch on the same GPU without
  coordinating with each other. The agent-facing outcome is one successful
  launch and one refusal. The exact simultaneous atomic acquire race is proven
  by the supporting acquire test below, not by relying on Codex startup timing.

Invariants and implementation pressure:

- [ ] There must be one reservation boundary responsible for path validation,
  symlink policy, canonical keys, metadata shape, and atomic acquire. A module
  such as `gpu_mcp_reservations.py` is likely, but the acceptance test owns the
  boundary.
- [ ] The lock authority is atomic directory creation. JSON writes are metadata,
  not the reservation authority.
- [ ] Shared metadata writes must be atomic and sanitized; args, full script
  paths, and output paths must not be serializable there.
- [ ] The v1 launcher nonce is a random per-attempt value generated by the MCP
  server or managed launcher, never derived from command-line args. The
  `process_fingerprint` stored in shared metadata is an opaque non-sensitive
  identity token derived from that nonce and process identity facts; it is used
  only for matching/confirming the intended process.
- [ ] Initial shared metadata follows the ADR 0004 metadata contract from the
  first reservation implementation: `job_id`, repo, `script_name`, host/GPU,
  `owner_user`, `server_instance_id`, process identity fields, and lease
  timestamps. Process identity fields may be null only during the launch window.
- [ ] Launch must reserve before remote launch. If a test can observe a launch
  without a reservation, the implementation is wrong.
- [ ] Launch returns a managed background job handle for both `async_mode=True` and
  `async_mode=False`.
- [ ] Existing sync-mode timeout tests must be rewritten around managed
  launch/status behavior, not preserved by keeping a second inline path.
- [ ] Stale-policy refusal remains ahead of reservation acquisition and launch.
- [ ] If launch fails before any process exists, remove the reservation. If
  launch outcome is uncertain, leave the reservation fail-closed.

Supporting tests:

- [ ] Key canonicalization and host validation.
- [ ] Atomic acquire race between distinct server instances or processes sharing
  the injected registry.
- [ ] Fake launch creates stable fake process identity and the harness can clean
  up fake process/reservation state after the scenario.
- [ ] Launch failure before any remote process exists removes the reservation.
- [ ] Launch outcome uncertainty, such as client disconnect after remote launch
  may have started, leaves the reservation fail-closed.
- [ ] Metadata sanitization.
- [ ] Malformed metadata treated as occupied/fail-closed.
- [ ] `async_mode=False` compatibility behavior.

## Phase 2: Fresh Reservations And Heartbeat Health

User-visible requirement: a live owner keeps its reservation fresh, and other
agents do not override it. If the local heartbeat manager is unhealthy, owner
actions that would change GPU jobs are refused, but status and reporting still
work when they can. Failure is part of the feature here: broken heartbeat
writes, broken registry reads, and server restarts must make the system more
conservative, not more eager to reuse the GPU.

Agentic battlefield first:

- [ ] Repo A launches a long-running job. Repo B calls `check_gpus` many times.
  Repo B always sees the GPU as reserved while Repo A's heartbeat is fresh, even
  if the fake GPU utilization is low. Fresh uninspected reservations report the
  conservative `RESERVED` state, not `RESERVED_IDLE`.
- [ ] Repo A launches a job, then its heartbeat writes start failing. Repo A asks
  for status and still gets a useful report. Repo A then tries to change a GPU
  job or launch a new one, and the tool refuses with a clear explanation: this
  server cannot safely manage leases until heartbeat writes recover.
- [ ] The harness pauses Repo A heartbeat writes between tool calls, while the
  reservation still exists. Repo B calls `check_gpus`. Repo B must not treat the
  GPU as free unless the later stale-cleanup rules prove the original process is
  gone.
- [ ] The harness turns Repo A's heartbeat write failure off and waits for a
  successful heartbeat write. Repo A is then allowed to manage GPU jobs again.
- [ ] Repo B cannot read the shared reservation registry. Repo B must not fall
  back to plain `nvidia-smi` and report the GPU as available. It returns the
  Phase 0 fail-closed shape: top-level `registry_status: "unavailable"` with
  per-GPU `availability: "unknown_unavailable"`.
- [ ] Repo A's MCP server restarts in the same repo while the old reservation
  still exists. The new server can report the old reservation, but it does not
  take over the heartbeat or change that old job.
- [ ] Repo A owns two managed jobs. Status for each job stays separate, and the
  agent can see that each job has its own heartbeat and next-check guidance.

Invariants and implementation pressure:

- [ ] Repo-local managed job records must exist for sensitive current-repo data
  and agent reminder state; shared metadata must stay minimal.
- [ ] The heartbeat owner is the current `server_instance_id`; new server
  instances do not adopt old reservations.
- [ ] Only owned reservations may be heartbeated.
- [ ] Heartbeat cadence is per task, not per agent.
- [ ] The cleanup finalization guard exists before cleanup behavior is
  implemented because heartbeat writes already need to share it. It is a
  short-lived per-reservation OS advisory lock, for example a lock file under
  `<registry-root>/.locks/<reservation_key>.lock`. It is never the reservation
  authority, never a persistent ownership marker, and must be automatically
  released on process death.
- [ ] Owner-side metadata writes need one update path that serializes
  owner-process writes, re-reads latest metadata, merges intended field changes,
  and writes atomically. This protects against heartbeat/status lost updates.
  Phase 7 `update_cadence` uses this same owner-side metadata path.
- [ ] Same-process owner writes should use an in-process lock per reservation key.
  Cross-process coordination with observer cleanup uses the cleanup finalization
  guard defined above, not separate whole-file writers.
- [ ] Heartbeat writes acquire the per-reservation in-process lock first, then
  briefly acquire the cleanup finalization guard around the metadata
  re-read/merge/replace. Cleanup acquires only the finalization guard after
  remote inspection, then re-reads and renames under that guard. Locks must be
  acquired in this order so heartbeat/status writes and cleanup cannot deadlock.
- [ ] The update path is a boundary the tests may force to move or split, but
  heartbeat, status, launch, retry, stop, and finish must not write stale
  whole-file snapshots independently.
- [ ] Stale threshold uses the interval written with the last successful
  heartbeat.
- [ ] Heartbeat write failures make owner mutations fail closed, but the manager
  can recover after later successful writes.
- [ ] Registry read failures are different from heartbeat write failures. If the
  registry cannot be read, tools must not infer availability from `nvidia-smi`
  alone.
- [ ] Status read/report behavior still works when heartbeat health is bad.
  Optional persistence such as `next_poll_after` or cadence updates may be
  skipped or degraded when local writes are unhealthy.
- [ ] The heartbeat worker follows the MCP server lifecycle by using an explicit
  stop event plus bounded sleeps. In production it must be a background thread
  that cannot keep the process alive after stdio transport exits; normal server
  shutdown, `KeyboardInterrupt`, `SIGTERM`, and test fixture teardown must set
  the stop event and perform only a bounded join. It must not spawn an
  unmanaged child process or rely on a separate daemon.
- [ ] Test-only heartbeat pause, fake-clock, and write-failure controls must be
  available only through the injected test backend/configuration. They must not
  be reachable from production policy or normal agent-facing MCP tools.
- [ ] Tests must distinguish heartbeat pause with a still-live server from full
  server death/restart. The expected owner-side behavior differs, even though
  observers still treat stale reservations conservatively.

Supporting tests:

- [ ] Heartbeat write/update.
- [ ] Lost-update regression between heartbeat and status/lifecycle writes, using
  a barrier/latch around metadata writes rather than relying on Codex timing.
- [ ] Health failure and recovery, including a test-controlled switch that turns
  write failure off and waits for a successful heartbeat.
- [ ] Registry-read failure does not fall back to unsafe availability.
- [ ] Per-task heartbeat isolation for two jobs owned by the same server,
  including optional per-reservation write-failure injection.
- [ ] Non-adoption by a new `server_instance_id`.
- [ ] Owner-action refusal while heartbeat manager is unhealthy.

## Phase 3: Stale Cleanup Requires Process Proof

User-visible requirement: a dead owner does not strand a GPU forever when the job
is gone, but no observer frees a GPU while the original job is still alive.

Agentic battlefield first:

- [ ] Repo A launches a known long-running fixture job. The harness stops Repo
  A's owner server or seeds equivalent old-server metadata, then ages the
  heartbeat while leaving the remote process alive. Repo B calls `check_gpus`
  through MCP and still sees the GPU as unavailable.
- [ ] Repo A launches a known fixture job. The harness stops Repo A's owner
  server or seeds equivalent old-server metadata, then lets the job exit by
  itself, or terminates only that harness-launched PID after
  fingerprint/start-time confirmation. Repo B calls `check_gpus`, the old
  process is proven gone, the stale reservation is moved aside, and only then a
  new launch can reserve the GPU.
- [ ] The harness makes inspection report the GPU host as unreachable. Repo B
  calls `check_gpus` and reports the GPU as not safely available.
- [ ] The harness seeds an old stale reservation that never recorded a remote
  PID. Repo B calls `check_gpus`; because the system cannot prove whether a
  process was launched, it keeps the GPU unavailable and tells the agent that
  manual cleanup may be needed.

Invariants and implementation pressure:

- [ ] Process proof must be centralized enough that cleanup, status, retry,
  finish, launch reconciliation, and rescue kill do not drift into different
  PID-reuse semantics.
- [ ] Inspection facts include PID existence, owner, process state, start time,
  host boot ID, non-sensitive fingerprint/nonce, and GPU attachment.
- [ ] `kill_gpu_process` should converge on the same inspection semantics while
  preserving its external shape as much as practical.
- [ ] PID reuse, owner mismatch, boot mismatch, fingerprint mismatch, no process
  at the stored non-null PID, and zombie state count as original-process-gone.
  Null `remote_pid` with unknown launch outcome remains fail-closed.
- [ ] `nvidia-smi` is diagnostic only; absence from GPU is not proof of
  process exit.
- [ ] Cleanup protocol must re-read metadata after inspection, abort if metadata
  changed or heartbeat refreshed, then acquire a short-lived per-reservation
  cleanup finalization guard shared with heartbeat writes as defined in Phase 2,
  re-read again under that guard, and only then atomic rename to `.quarantine/`.
- [ ] If the guard uses a process-scoped lock such as `fcntl.lockf`, guard tests
  must exercise owner heartbeat and observer cleanup from separate processes.
  A same-process two-thread test is not enough to prove cross-process exclusion.
- [ ] `check_gpus` overlays reservations and inspects only stale entries
  within a bounded budget. Use the companion defaults unless a test forces a
  narrower value: 10 seconds per host inspection, at most 2 stale GPU
  inspections per host, at most 5 stale reservation inspections per tool call,
  and a 30 second total `check_gpus` timeout.
- [ ] Stale entries skipped because the inspection budget is exhausted remain
  unavailable and are reported as stale-uninspected/unknown-reserved, never
  `AVAILABLE`.
- [ ] If an existing reservation is cleanable stale-gone during launch, clean it
  and retry atomic acquire. Cleanup itself never transfers ownership.

Supporting tests:

- [ ] Zombie process treated as gone.
- [ ] Harness can launch and identify test-owned remote processes, and may kill
  only those processes after fingerprint/start-time confirmation.
- [ ] Harness can stop an owner server, age heartbeat/fake time without waiting
  real stale thresholds, and leave the remote fixture process alive.
- [ ] Harness can seed stale metadata with null `remote_pid`.
- [ ] Harness can simulate host-unreachable inspection failure.
- [ ] PID reuse and boot-ID mismatch.
- [ ] Matching live process off-GPU remains reserved.
- [ ] Unknown host/inspection failure remains unavailable.
- [ ] Cleanup race re-read abort.
- [ ] Cleanup finalization guard prevents heartbeat refresh between final re-read
  and quarantine rename. This regression must use separate owner/observer
  processes when the chosen guard primitive is process-scoped.
- [ ] Stale entries skipped because the inspection budget is exhausted remain
  unavailable/stale-uninspected, never available.
- [ ] Two cleaners racing: one quarantine, one clean abort.
- [ ] Null `remote_pid` fail-closed case.

## Phase 4: Managed Status, Recovery, And Agent Discipline

User-visible requirement: after launch, the agent follows the job through
`manage_gpu_job(status)`, recovers lost context by repo when unambiguous, and is
told not to proceed with output-dependent work before terminal status.

Agentic battlefield first:

- [ ] Repo A launches `hold_gpu.py` and asks for status. The answer says whether
  the job is still running, where to look for logs/results, when to check again,
  that output-dependent work must wait, and that independent work may continue
  until the next poll.
- [ ] Repo A launches `quick_success.py` and `quick_fail.py` in separate runs.
  After each finishes, status reports success or failure from the managed
  launcher outcome record, and returns a log pointer or bounded tail containing a
  known output marker.
- [ ] The harness creates a fault case where the process is gone but the
  repo-local outcome record is missing, malformed, or unreadable. Repo A asks
  status through MCP and sees unknown outcome rather than guessed success or
  failure.
- [ ] Repo A launches a long-running job, then asks for status without passing
  the `job_id` while the same MCP server still owns exactly one current-repo
  reservation. The tool recovers that one target by repo and reports it.
- [ ] Repo A launches a long-running job in one `codex exec` session, then that
  session exits and the old MCP server is gone. The harness keeps the remote
  process alive and makes the old heartbeat stale. A second `codex exec` session
  in the same repo asks for status/list recovery. If exactly one active
  reservation exists for that repo, the new server can find and report it, but it
  does not take over its heartbeat. Stale-gone cleanup is tested in Phase 3, not
  in this recovery case.
- [ ] Repo A has multiple active reservations, either from two cheap fixture jobs
  when resources allow or from harness-seeded current-repo reservations. The
  tool lists safe candidates and asks the agent to choose; it does not guess
  which one the agent meant.

Invariants and implementation pressure:

- [ ] There must be a read-only reservation listing surface, likely
  `list_gpu_reservations(scope="mine"|"all", fresh=False)`, where `fresh` has
  the Phase 0 meaning: bounded refresh inspection, not filtering.
- [ ] There must be a managed status surface, likely
  `manage_gpu_job(action="status", job_id=None, reservation_key=None)`.
- [ ] Target resolution order is fixed: `job_id`, then `reservation_key`, then exactly
  one current-repo reservation.
- [ ] Ambiguous resolution returns sanitized candidates and never guesses by
  recency, host, GPU index, state, or script name.
- [ ] Initial `next_poll_after` is persisted at launch; revised
  `next_poll_after` is persisted when status computes a new suggested poll time.
- [ ] Outcome recording must be good enough to separate success, failure/signal,
  and process-gone-with-unknown-outcome.
- [ ] The managed launcher, not the long-running MCP tool call, is responsible for
  writing the repo-local outcome record at
  `<repo>/.gpu_mcp_state/jobs/<job_id>/attempts/<attempt_id>/outcome.json` using
  the Phase 0 JSON contract. If status later sees process-gone without the
  active attempt's outcome record, it reports
  process-gone-with-unknown-outcome.
- [ ] The managed launcher/wrapper must record outcome after the remote process
  exits; success/failure status cannot be inferred later from `ps` alone.
- [ ] If the repo-local outcome record exists but is malformed or unreadable,
  status treats it like a missing outcome record and reports
  process-gone-with-unknown-outcome rather than guessing.
- [ ] Current-repo output/log pointers come only from repo-local job records.
- [ ] Stale-policy refusal applies to `manage_gpu_job` and
  `list_gpu_reservations` before normal status/listing/lifecycle behavior.

Supporting tests:

- [ ] Target resolution order.
- [ ] Zero/multiple recovery candidates.
- [ ] Sanitized candidate fields.
- [ ] Terminal outcome mapping.
- [ ] Missing/corrupt outcome fault injection maps to
  process-gone-with-unknown-outcome.
- [ ] Status works when local heartbeat manager is unhealthy.
- [ ] Status for old-server reservations is read/report only, not adoption.
- [ ] Stale-policy refusal for `manage_gpu_job` and `list_gpu_reservations`.

## Phase 5: Changing Or Ending Jobs

User-visible requirement: when an agent changes its mind about a job it owns,
the tools must keep two things separate: the remote process and the GPU
reservation. Sending a signal is not the same as freeing the GPU, and retrying a
job is not the same as starting an unrelated new job somewhere else.

Agentic battlefield first:

- [ ] Repo A launches `hold_gpu.py`, then asks to retry the same managed job while
  the previous attempt is still running. The tool refuses because retry would
  create a second process on the same reserved GPU. The message tells the agent
  to keep checking status, or to explicitly stop or otherwise terminate the
  still-live attempt before retry can relaunch.
- [ ] Repo A tries to launch on GPU 0 but is refused because GPU 0 is already
  reserved. Repo A then polls, finds GPU 1 available, and launches there through
  normal reservation acquisition. There is no GPU 0 lifecycle decision for Repo A
  because Repo A never launched a job on GPU 0.
- [ ] Repo A already has a running managed job on GPU 0, then decides to launch a
  separate managed job on GPU 1 as well. That is allowed if GPU 1 can be
  reserved. This is a separate launch, not a retry of the GPU 0 job.
- [ ] Repo A launches `term_delay.py`, then stops its owned job. The tool sends
  the signal, but the GPU stays reserved while the process is exiting. Other
  agents still see that GPU as unavailable. Owner status may report that stop was
  requested and whether the process is still alive or already gone.
- [ ] After the stopped process is actually gone, Repo A retries the same managed
  job. The replacement starts under the same reservation and records the new
  process identity.
- [ ] Repo A launches `quick_success.py`, waits until status shows the process is
  gone, then finishes. The reservation is removed immediately.
- [ ] Repo A finishes a still-live fixture job. The server refuses, keeps the
  heartbeat active, and tells the agent to call `stop` first if it intends to
  terminate the job, or keep polling `status` if it intends to wait.
- [ ] Repo A or Repo B uses the rescue path for a known fixture PID such as
  `ignore_term.py`. `kill_gpu_process` first inspects one host/PID and returns a
  fingerprint; a second call may signal only with that returned fingerprint, and
  signaling does not remove the reservation.

Invariants and implementation pressure:

- [ ] Job-changing actions likely live under `manage_gpu_job`, but the important
  property is that only the owning server may change its own jobs.
- [ ] All owner-side job-changing actions refuse when heartbeat manager is
  unhealthy.
- [ ] `retry` must inspect first and refuse while the matching process is alive.
  If the process is already gone, retry may relaunch under the same reservation.
  If the caller wants to abandon a still-live attempt, it must explicitly choose
  stop or another termination path before retry can relaunch.
- [ ] Retry refusal is scoped to the same reservation. It must not imply that the
  agent is forbidden from launching a separate new job on a different GPU after a
  fresh `check_gpus`/reservation acquisition.
- [ ] `finish` inspects immediately. If the matching process is gone, it removes
  the reservation. If the matching process is live, it refuses, keeps heartbeat
  active, and tells the agent to call `stop` first or continue polling.
- [ ] Rescue kill remains separate from lease ownership and cleanup.

Supporting tests:

- [ ] Owner mutation refused for a non-owned reservation.
- [ ] Owner mutation refused while the heartbeat manager is unhealthy.
- [ ] Retry live-process refusal.
- [ ] Retry after gone/stopped.
- [ ] Finish gone immediate cleanup.
- [ ] Finish alive leaves reservation occupied until stale-plus-process-gone
  cleanup.
- [ ] Stop does not free reservation.
- [ ] Stop-then-zombie is treated as process gone for status/retry.
- [ ] Zombie handling is covered by a fake process-table test unless a reliable
  special zombie fixture is added later.
- [ ] Kill does not remove reservation.
- [ ] Stale-policy refusal for `kill_gpu_process`.

## Phase 6: Hook Reminder Path

User-visible requirement: the agent gets due-job reminders during normal tool
use without making the hook a scheduler, remote inspector, or safety boundary.

Agentic battlefield first:

- [ ] First run an automated Codex hook capability probe against the installed
  global GPU MCP companion hook. The pytest battlefield harness sets a
  pytest-only nonce environment variable that makes the real installed
  `PreToolUse` hook emit `hookSpecificOutput.additionalContext`, exposes one
  harmless MCP tool from the simulated backend, and runs `codex exec` with a
  prompt that calls that tool and reports any model-visible hook context. The
  nonce must appear nowhere else in the prompt or fixture files. Passing means
  the final answer contains the nonce after the ordinary tool call. If the probe
  fails, rerun once with an explicit instruction to report hook context; if that
  also fails, mark hook reminders off for v1 and rely on launch/status tool
  output.
- [ ] Repo A launches a managed fixture job through MCP. The harness makes the
  job's `next_poll_after` due by advancing fake time or using a short test
  interval. Before Repo A's next ordinary tool call, `PreToolUse` gives the agent
  one status-check reminder visible to the model.
- [ ] The reminder tells the agent the practical behavior: call status when
  appropriate, do not use outputs before the job is terminal, and continue only
  with work that does not depend on those outputs.
- [ ] Repo A keeps using tools without checking status. The hook does not repeat
  the exact same reminder over and over for the same due time.
- [ ] Repo A has a due managed job, then Repo B runs an ordinary MCP tool call
  from its own repo. Repo B must not receive Repo A's reminder, because the
  global hook scopes itself to the nearest current-repo `gpu-mcp.toml` and
  current-repo job state.
- [ ] After Repo A uses status/list recovery to make a previous-server
  reservation locally known, the hook may emit a status-only reminder. It must
  not discover old reservations by scanning, and it must not suggest retry or
  finish because this server does not own that reservation.
- [ ] Repo A has both a stale policy problem and a due heartbeat reminder. The
  stale-policy block wins, because policy safety is not weakened by reminders.

Invariants and implementation pressure:

- [ ] The hook likely extends `gpu_mcp_policy_hook.py` as an ordered dispatcher:
  stale-policy block first, heartbeat reminder second.
- [ ] Stale-policy hook failures fail closed and block the tool call; heartbeat
  reminder failures fail quiet and must not weaken the policy boundary.
- [ ] Hook reminders emit only from `PreToolUse`; launch/status tool output still
  carries polling guidance independently of hooks.
- [ ] Codex `hookSpecificOutput.additionalContext` is the v1 output mode.
- [ ] Phase 6 battlefield uses a real Codex hook path. The agent uses only MCP
  tools; the harness uses the installed global companion hook, may run
  `codex exec` or interactive Codex, and may advance test time or set a short
  test interval. Direct edits to repo-local job state or hook-owned advisory
  state are supporting-test setup, not agent behavior.
- [ ] The heartbeat reminder branch uses the same injected clock source as the
  MCP test backend. Production uses wall-clock time.
- [ ] The global hook scopes itself by walking upward from the current hook
  working directory to the nearest `gpu-mcp.toml`. The heartbeat reminder branch
  reads only that current repo's job state and shared reservation metadata for
  those current-repo records. The stale-policy branch may read the discovered
  current repo's policy state.
- [ ] If the upward walk finds no `gpu-mcp.toml`, the user-global hook exits
  quietly and emits no advisory context.
- [ ] Reminders match by `job_id`, `reservation_key`, and the local/recovery
  record's recorded owner `server_instance_id` against shared metadata.
- [ ] A non-owned recovered reservation may receive only a status reminder.
  Retry/finish suggestions require the recorded owner `server_instance_id` to
  match the current server instance and must still pass normal ownership checks.
- [ ] The hook never SSHes, calls MCP tools, or inspects remote processes.
- [ ] The MCP server owns the repo-local job record; the hook writes only separate
  hook-owned advisory reminder state, such as
  `<repo>/.gpu_mcp_state/hook_reminders/<job_id>.json`, to avoid lost updates
  between hook and server writes.
- [ ] Marking a reminded due timestamp in hook-owned advisory state needs
  cross-process file atomicity or fail quiet; hook processes are separate from
  the MCP server process.
- [ ] The hook tolerates reservation disappearance while reading shared metadata.
  Missing reservation directories or metadata files suppress the reminder without
  raising.
- [ ] The hook does not scan the shared registry to discover old jobs by itself
  and must not emit reminders for another repo's jobs. For a non-owned
  current-repo reservation, it may emit a status-only reminder only if that
  reservation is already known through current-repo state or a prior status/list
  recovery result. It must not create a full managed job record or adopt
  heartbeat ownership just to remind.
- [ ] v1 reminder output modes are `additionalContext` or off.

Supporting tests:

- [ ] Due reminder emits context.
- [ ] Due reminder includes the required status-check and output-dependency
  discipline.
- [ ] No duplicate due timestamp.
- [ ] Cross-repo isolation: Repo A due-job state is silent when the hook fires
  from Repo B.
- [ ] Live Codex battlefield cross-repo isolation: Repo A has a due managed job,
  Repo B runs an ordinary MCP tool call, and Repo B sees no Repo A reminder.
- [ ] Non-GPU-MCP repo quiet exit: the user-global hook fires from a directory
  with no ancestor `gpu-mcp.toml` and emits no advisory context.
- [ ] Future poll is silent.
- [ ] PostToolUse is silent.
- [ ] Stale policy block wins.
- [ ] Malformed/locked state fails quiet.
- [ ] Missing reservation retires advisory reminder.
- [ ] Stale local reminder state is retired when the shared reservation is gone
  or no longer matches.

## Phase 7: Dynamic Cadence

Do not implement this until the fixed-cadence lifecycle and Phase 6 hook
delivery are correct, and ADR 0004p's public cadence input interface is
accepted.

User-visible requirement: long jobs should not force frequent polling, and short
smoke probes should be checked soon, without making runtime estimates cleanup
authority. Phase 7 also provides poll discipline: accidental early status checks
should be compact and cheap, while intentional early checks remain possible with
an explicit reason.

Detailed agentic polling test design lives in
`0004r-phase7-agentic-polling-tests.md`. This phase uses deterministic
server/hook tests for contract facts, plus Codex battlefield traces and an LLM
judge for behavior that cannot be made fully deterministic.

Phase 6 baseline behavior tests before Phase 7 implementation. These are
expected-red contrast tests when the current agent still babysits jobs; a
preserved hard-fail report is a useful result, not an implementation surprise:
they are the A side of the A/B test. If a baseline passes, inspect the trace
before trusting it; the agent may have genuinely behaved well, or the prompt and
oracle may have been too weak to expose the missing Phase 7 harness.
The pytest control cases may pass by detecting this hard-fail verdict; the
future Phase 7 acceptance cases are the B side that must pass by showing the
agent follows the new cadence guidance.

- [ ] Live Codex baseline: final-result wait. The prompt uses only current Phase
  6 MCP arguments and asks Codex to run a finite slow job and report the final
  metric. The test records whether Codex repeatedly checks status while the job
  is still running, blocks in shell `sleep`, uses broad GPU checks as polling,
  or treats started/progress files as final output before terminal MCP status.
- [ ] Live Codex baseline: final-result wait plus independent local work. The
  prompt uses only current Phase 6 MCP arguments, asks for a final GPU metric,
  and gives a concrete local note/prep task to do while the GPU job runs. The
  test requires a local proof file and records whether Codex still spends the
  wait polling.
- [ ] Live Codex baseline: smoke opportunity without Phase 7 fields. The script
  has a quick mode and a finite real mode. The prompt asks for the real final
  metric and mentions the quick check mode. The test watches whether Codex runs
  and checks the quick mode before launching the long mode, then whether it
  repeatedly status-checks the real run while waiting.
- [ ] Live Codex baseline: two final-result jobs through current MCP. The test
  watches whether Codex keeps both job ids and final metrics separate without
  multiplying repeated running-status checks across jobs.
- [ ] Live Codex baseline: current Phase 6 due reminder and cross-repo silence.
  The due repo should act on its own reminder; another repo should not see or
  act on that reminder. The other repo may still see the shared reservation in
  broad `check_gpus` output; that is not a failure unless due-reminder context
  or targeted status handling leaks across repos.
- [ ] Baseline tests must not fake Phase 7 API replies or pass Phase 7-only
  arguments such as `job_role`, `smoke_job_id`, `smoke_skip_reason`,
  `expected_duration_sec`, `cadence_hint_sec`, `early_poll_reason`, or
  `update_cadence`.
- [ ] Baseline behavior verdicts must first prove a real managed launch with
  `job_id` and `next_poll_after`; an empty trace or missing final-answer JSON is
  inconclusive or failing, never a pass.
- [ ] Baseline behavior verdicts compare the captured Codex stream against the
  reported final MCP history. Final metrics must be reported separately from raw
  MCP results, so a metric number embedded in tool JSON does not count as the
  agent reporting the result.

Agentic battlefield first:

- [ ] Live Codex prompt: Repo A asks the agent to launch a likely main/long job
  without `job_role`, positive smoke viability evidence, or
  `smoke_skip_reason`. Because `async_mode=True` defaults the role to `main`,
  the PreToolUse hook injects a structured precondition: run a representative
  bounded `job_role="smoke"` job first with small inputs, max steps, dry run, or
  a short smoke script, or retry the main launch with a concrete
  `smoke_skip_reason`.
- [ ] Repo A calls `run_python_on_gpu` with `async_mode=True` and no
  `job_role`. The launch contract resolves `job_role="main"`, reports that the
  role was defaulted, and soft-refuses if no positive smoke viability evidence
  or `smoke_skip_reason` is present.
- [ ] Repo A calls `run_python_on_gpu` with `async_mode=False` and no
  `job_role`. The launch contract resolves `job_role="one_off"` and reports
  that the role was defaulted, without treating the compatibility flag as proof
  of short runtime.
- [ ] Live Codex prompt: Repo A supplies representative smoke arguments. The
  agent launches `job_role="smoke"` with an explicit short
  `expected_duration_sec` or `cadence_hint_sec`; launch output exposes
  `heartbeat_interval_sec`, `next_poll_after`, and a smoke cadence basis, with
  the interval selected by the pinned coarse-band contract and clamped no lower
  than the minimum.
- [ ] Repo A launches `job_role="smoke"` without explicit cadence input. The
  smoke job still creates a normal reservation and heartbeat, and its first
  `heartbeat_interval_sec`/`next_poll_after` use the minimum interval.
- [ ] Live Codex prompt: Repo A's main script has no built-in small-run flag.
  The agent creates or adapts a smoke path, such as a small input fixture, an
  added smoke mode, or a separate managed smoke script that exercises the same
  relevant GPU code path, then launches it through MCP as `job_role="smoke"`.
- [ ] Live Codex prompt: after the smoke result, the agent checks
  `manage_gpu_job(status)` for the smoke job. When the smoke job succeeds,
  status output reports the smoke `job_id`, observed runtime when available, and
  the next-call shape for launching the main job with `job_role="main"` and
  `smoke_job_id`.
- [ ] Repo A launches `job_role="main"` with a valid same-repo successful
  `smoke_job_id` but no explicit cadence evidence. Launch output cites the smoke
  job as viability evidence in `cadence_basis` and uses the conservative minimum
  first poll cadence instead of extrapolating from smoke runtime.
- [ ] Repo A launches `job_role="main"` with a valid same-repo failed, running,
  or unknown-outcome `smoke_job_id`. Launch output records the smoke lifecycle
  in `cadence_basis` but does not describe it as positive viability evidence,
  and soft-refuses unless the launch also has `smoke_skip_reason`. If the agent
  retries with `smoke_skip_reason`, cadence is conservative unless another
  cadence signal is supplied.
- [ ] Repo A launches `job_role="main"` with a valid same-repo successful
  `smoke_job_id` plus `smoke_cadence_representative=true`,
  `expected_duration_sec`, or `cadence_hint_sec`. Launch output may use the smoke
  runtime or explicit cadence signal in `cadence_basis` and tells the agent when
  to check next.
- [ ] Repo A launches `job_role="main"` with a missing, cross-repo, wrong-role, or
  unreadable-lifecycle `smoke_job_id`. The tool refuses to use it as smoke
  evidence.
- [ ] Repo A launches a main/long job with an explicit long
  `expected_duration_sec` or `cadence_hint_sec`, successful smoke or
  `smoke_skip_reason`, and no stronger cadence input. A direct
  `cadence_hint_sec` wins and clamps to `60..3600`; otherwise
  `expected_duration_sec` maps through the pinned coarse duration bands. The
  tool gives a later next-check time no higher than the maximum interval.
- [ ] Repo A launches `job_role="main"` with `smoke_skip_reason` but no
  `expected_duration_sec` or `cadence_hint_sec`. Launch output records the skip
  reason in `cadence_basis` and uses the conservative minimum first poll cadence.
- [ ] During polling, Repo A observes that the initial cadence is wrong and calls
  `manage_gpu_job(action="update_cadence", job_id=...,
  expected_duration_sec=... or cadence_hint_sec=..., reason=...)`. The server
  updates cadence and heartbeat together, and the next status output reports the
  revised basis.
- [ ] When a running job is due, the hook reminder tells the agent to
  status-check and optionally update cadence if observed progress contradicts
  the old cadence. It must not perform the status check or write cadence itself.
- [ ] Repo A launches a long managed job and receives a future
  `next_poll_after`. Before that time, the agent calls
  `manage_gpu_job(action="status", job_id=...)` without an
  `early_poll_reason`. The server returns compact
  `polling_state="not_due_yet"` guidance and does not inspect the remote
  process, tail logs, acknowledge a reminder, or move `next_poll_after`.
- [ ] Repo A has a concrete reason to check early, such as a user request or an
  output dependency, and calls `manage_gpu_job(action="status", job_id=...,
  early_poll_reason=...)` before `next_poll_after`. The server performs full
  status and records or reports the intentional override.
- [ ] Repo A calls `manage_gpu_job(status)` after `next_poll_after` is due. The
  server performs full status without requiring `early_poll_reason`.
- [ ] Repo A has a local terminal outcome before `next_poll_after`, including a
  smoke job that finished quickly. A status call reports terminal full status
  instead of hiding the result behind `polling_state="not_due_yet"`.
- [ ] Before a premature targeted
  `manage_gpu_job(action="status", job_id=...)` call, the PreToolUse hook may
  warn that the job is not due and explain `early_poll_reason`. The warning is
  advisory only and must not fire for `check_gpus`, `list_gpu_reservations`,
  unrelated tools, cross-repo jobs, ambiguous/malformed targets, due jobs,
  terminal jobs, lifecycle actions, status calls with `early_poll_reason`, or
  `PostToolUse`.
- [ ] Battlefield trace harness: each Codex polling-discipline scenario records
  JSONL events for prompt, tool calls, compact tool-result summaries, final
  answer, machine checks, and optional judge result. The trace is the primary
  artifact for reviewing agent behavior.
- [ ] Battlefield machine checks run before any judge call and hard-fail clear
  facts: no raw GPU access, no repeated early polling, no output claims before
  terminal status, smoke job attempted when a small mode is offered, and
  independent work performed when the prompt gives useful independent work.
- [ ] Battlefield judge review receives only the scenario prompt, trace/final
  answer, and machine-check summary. The judge evaluates agent behavior such as
  premature polling, handling of `not_due_yet`, use of `early_poll_reason`,
  output-dependency discipline, and due-reminder handling. Judge verdicts are
  advisory until repeated runs show stability.
- [ ] Battlefield scenarios implement ADR 0004r's first behavior contrast group:
  final-result wait, final-result wait plus independent local work, smoke
  opportunity followed by a real finite main run, two final-result jobs, and
  due-reminder/cross-repo silence. A no-obvious-smoke-path prompt may start as a
  manual copied-prompt test before it becomes stable under `codex exec`.

Invariants and implementation pressure:

- [ ] `run_python_on_gpu` accepts public cadence evidence:
  `job_role="smoke"|"main"|"one_off"`, optional `expected_duration_sec`,
  optional `cadence_hint_sec`, optional `smoke_job_id`, optional
  `smoke_cadence_representative`, and optional `smoke_skip_reason`.
  `preflight` is prose only; the public wire role is `smoke`.
- [ ] If `job_role` is omitted, `async_mode=True` defaults to `job_role="main"`
  and `async_mode=False` defaults to `job_role="one_off"`. Explicit `job_role`
  always wins, and launch output reports whether the role was defaulted.
- [ ] A `job_role="main"` launch with no positive smoke viability evidence and
  no `smoke_skip_reason` soft-refuses with guidance to run smoke first or
  provide a skip reason. `expected_duration_sec` and `cadence_hint_sec` are
  cadence evidence only; they do not explain why smoke was skipped.
- [ ] Launch and status output expose the chosen `heartbeat_interval_sec`,
  `next_poll_after`, and `cadence_basis`.
- [ ] `manage_gpu_job(action="status")` accepts optional `early_poll_reason`.
  Blank or whitespace-only values behave as absent. The reason is repo-local
  observability data, not a safety proof.
- [ ] A status call is due when its current repo-local `next_poll_after` is
  parseable and `now >= next_poll_after`. If `next_poll_after` is missing or
  malformed, status must take the full status path rather than hiding behind
  `not_due_yet`.
- [ ] Before `next_poll_after`, a targeted status call for a known nonterminal
  current-repo job with no `early_poll_reason` returns compact
  `polling_state="not_due_yet"`. This response is local-only and is not a
  lifecycle assertion; it must not imply the remote process is alive.
- [ ] `polling_state="not_due_yet"` includes `job_id`, `reservation_key` when
  known, `heartbeat_interval_sec`, `next_poll_after`, `seconds_until_due`,
  `full_status_performed=false`, `remote_inspection_performed=false`,
  `log_tail_included=false`, and concise guidance to do independent work or
  retry with `early_poll_reason` for an immediate full check.
- [ ] `polling_state="not_due_yet"` must not SSH, inspect the remote process,
  tail logs, update `last_status_checked_at`, advance `next_poll_after`, renew
  heartbeat, change cadence, update process-inspection state, or acknowledge a
  hook reminder.
- [ ] Full status is required when the status call is due or overdue, has
  non-empty `early_poll_reason`, has a local terminal outcome, has missing or
  malformed cadence state, has ambiguous target resolution, or needs
  ownership/policy/recovery diagnostics. A local terminal outcome before
  `next_poll_after` wins over `not_due_yet`.
- [ ] An early status call with non-empty `early_poll_reason` performs full
  status and records an intentional override in repo-local state and/or output.
  If override recording fails, full status may still return with
  `early_poll_override_recorded=false` and a bounded warning.
- [ ] `select_cadence` is deterministic. Inputs are positive finite numeric
  seconds; booleans, non-numeric values, non-positive values, and non-finite
  values are invalid, and fractional values are rounded up before selection.
  Precedence is direct `cadence_hint_sec`, then `expected_duration_sec`, then a
  successful `smoke_job_id` with `smoke_cadence_representative=true`, then
  conservative no-evidence cadence.
- [ ] Expected duration and cadence-representative successful smoke runtime use
  the same coarse bands: `<=300` seconds selects `60`, `>300` and `<=1800`
  selects `180`, `>1800` and `<=7200` selects `600`, and `>7200` selects
  `1800`. Direct `cadence_hint_sec` is not banded; it is clamped to `60..3600`.
- [ ] `next_poll_after` is computed as `now + heartbeat_interval_sec` using the
  same owner-side timestamp as the heartbeat write and is returned as a UTC RFC
  3339 timestamp ending in `Z`. Terminal jobs omit it or return null.
- [ ] The cadence basis records whether launch had smoke viability evidence,
  smoke cadence evidence, explicit expected duration, direct cadence hint,
  conservative no-evidence cadence, or default cadence. Sensitive script paths
  and arguments stay repo-local under the existing metadata rules.
- [ ] `cadence_basis` is a structured object, not prose. It records fields such
  as `source`, `conservative_reason`, `smoke_job_id`, `smoke_lifecycle`,
  `smoke_runtime_sec`, `positive_viability_evidence`,
  `cadence_evidence_used`, `expected_duration_sec`, `cadence_hint_sec`,
  `smoke_cadence_representative`, `skip_reason_recorded`,
  `selected_interval_sec`, and `duration_band` when applicable.
- [ ] Phase 7 state storage is repo-local except for lease cadence. Shared
  metadata may contain `last_heartbeat_at` and `heartbeat_interval_sec`, plus
  existing sanitized reservation identity fields. `job_role`, `smoke_*`,
  `expected_duration_sec`, `cadence_hint_sec`, `smoke_skip_reason`, observed
  runtime, `cadence_basis`, `next_poll_after`, and output pointers stay in
  repo-local job state or current-repo tool output.
- [ ] `smoke_job_id` is a valid smoke reference when it names a same-repo
  managed job with `job_role="smoke"` and readable lifecycle/outcome. Success is
  required only before the server may describe it as positive viability evidence
  or use its runtime as cadence evidence.
- [ ] `smoke_job_id` is the normal smoke-result index. The server uses it to
  retrieve the repo-local smoke job record, lifecycle/outcome, runtime, and output
  pointers. Listing/searching recent smoke jobs is not required for Phase 7.
- [ ] A successful smoke status response gives explicit smoke-to-main guidance:
  preserve the smoke `job_id`, report observed runtime when available, and show
  that the main launch should pass `job_role="main"` and `smoke_job_id`. It may
  tell the agent to include `smoke_cadence_representative=true` if the agent
  judges the smoke runtime representative enough for cadence.
- [ ] Smoke status guidance includes a structured `recommended_next_call` object
  when applicable; prose guidance is secondary.
- [ ] `manage_gpu_job(action="update_cadence")` is owner-side, requires a
  reason, clamps the interval to `60..3600`, and writes
  `heartbeat_interval_sec` plus `last_heartbeat_at` in the same metadata update.
  It refuses for non-owned reservations, stale policy, unhealthy heartbeat
  manager, missing cadence input, missing/blank reason, or failed owner metadata
  update.
- [ ] Smoke/preflight evidence maps to initial cadence only when the probe is
  explicitly cadence-representative or paired with expected main-job duration.
  The MCP server does not invent smoke arguments or judge semantic
  representativeness.
- [ ] A smoke path may be reduced arguments for the target script, an added smoke
  mode, a tiny input fixture, a dry-run/max-steps path, or a separate managed
  smoke script that exercises the same relevant GPU code path. The harness must
  not confine this choice beyond requiring MCP launch and `job_role="smoke"`.
- [ ] Smoke jobs are not heartbeat-free. They use the normal reservation and
  heartbeat protocol because a smoke run can hang or overrun while holding a GPU.
  Without explicit smoke cadence input, first poll guidance uses the minimum
  heartbeat interval.
- [ ] Do not add smoke templates, smoke recipe files, or a separate smoke-only
  launch path. A smoke test is an ordinary managed GPU job submitted through the
  MCP launch tool.
- [ ] A likely main/long job without positive smoke viability evidence must
  record a non-empty `smoke_skip_reason`; expected duration and cadence hints
  affect polling but do not replace smoke or a skip reason. The hook nudges this
  and the launch tool may soft-refuse accidental omissions, but neither is a GPU
  safety boundary. The server records the reason but does not semantically grade
  it.
- [ ] A main launch with no cadence evidence, including a launch that has only
  `smoke_skip_reason` or only smoke viability evidence, uses the minimum
  heartbeat interval for the first poll. This is the operational cost of weak
  cadence evidence.
- [ ] Do not infer duration from comments, arbitrary stdout, `ps`, or
  `nvidia-smi` utilization patterns in v1.
- [ ] Intervals remain clamped to `60..3600` seconds.
- [ ] The same `select_cadence` contract applies to launch-time cadence and
  `manage_gpu_job(action="update_cadence")`.
- [ ] Runtime estimates remain non-authoritative for cleanup.
- [ ] The hook never acts as a scheduler and never writes heartbeat or cadence
  state. The Phase 7 hook branch runs after stale-policy handling, fails quiet,
  and must not veto or mutate tool input; server-side soft refusal is the
  workflow guard.
- [ ] The Phase 7 early-poll hook warning is narrow and advisory. It may only
  target unambiguous premature `manage_gpu_job(action="status", job_id=...)`
  calls for current-repo jobs without `early_poll_reason`; it must be silent for
  broad GPU tools, unrelated tools, cross-repo jobs, malformed or ambiguous
  targets, due jobs, terminal jobs, lifecycle actions, calls with
  `early_poll_reason`, and `PostToolUse`.

Supporting tests:

- [ ] Direct `cadence_hint_sec` clamps to `60..3600` and wins over
  `expected_duration_sec` and cadence-representative smoke runtime.
- [ ] Invalid cadence inputs are refused: booleans, non-numeric values,
  non-positive values, and non-finite values.
- [ ] Expected duration coarse-band boundaries:
  `300 -> 60`, `301 -> 180`, `1800 -> 180`, `1801 -> 600`, `7200 -> 600`,
  and `7201 -> 1800`.
- [ ] Cadence-representative successful smoke runtime uses the same coarse-band
  boundaries when no direct hint or expected duration is present.
- [ ] `next_poll_after` is derived from the same timestamp as the heartbeat
  write and equals `now + heartbeat_interval_sec` for nonterminal jobs.
- [ ] Malformed nonterminal `next_poll_after` values take the full status path
  instead of `not_due_yet`: missing field, null, empty string, invalid
  timestamp, timestamp without trailing `Z`, and timestamp with garbage suffix.
  Terminal jobs may still omit or null `next_poll_after`.
- [ ] Multi-job cadence is per task. Jobs with different `next_poll_after`
  values are reminded, early-warned, and status-gated independently.
- [ ] Early status before `next_poll_after` with no `early_poll_reason` returns
  compact `polling_state="not_due_yet"`, performs no remote inspection, includes
  no log tail, and does not mutate `last_status_checked_at`, `next_poll_after`,
  heartbeat, cadence, or process-inspection state.
- [ ] Compact `polling_state="not_due_yet"` does not acknowledge or silence a
  Phase 6 due reminder.
- [ ] Hook reminder de-duplication and advisory-state retirement are covered by
  deterministic tests per ADR 0004q; Phase 7 must not regress them.
- [ ] Early status with non-empty `early_poll_reason` performs full status and
  records or reports the override.
- [ ] Due status without `early_poll_reason` performs full status.
- [ ] Local terminal outcome before `next_poll_after` returns terminal full
  status, not compact `not_due_yet`.
- [ ] Missing or malformed `next_poll_after`, ambiguous target resolution, and
  policy/ownership/recovery diagnostics do not collapse into `not_due_yet`.
- [ ] Hook warns only for premature targeted
  `manage_gpu_job(action="status", job_id=...)` without `early_poll_reason`.
- [ ] Hook is silent for `check_gpus`, `list_gpu_reservations`, unrelated tools,
  cross-repo jobs, due jobs, terminal jobs, lifecycle actions, malformed or
  ambiguous targets, status calls with `early_poll_reason`, and `PostToolUse`.
- [ ] Battlefield: an agent that tries to poll early receives compact guidance
  and no log/inspection output unless it supplies `early_poll_reason`.
- [ ] Battlefield trace JSONL follows ADR 0004r's schema and excludes raw script
  arguments, sensitive paths outside the test repo, unbounded stdout/stderr, and
  full log tails.
- [ ] Battlefield machine checks are deterministic and run before the LLM judge;
  a machine-check failure is a hard test failure.
- [ ] LLM judge output follows ADR 0004r's strict JSON shape with
  `verdict`, five 0-3 behavior scores, trace-based `evidence`, and
  `failure_reason`.
- [ ] LLM judge prompt uses ADR 0004r's strict trace-based rubric: no credit for
  unsupported rationalizations, repeated polling after `not_due_yet` is a hard
  fail, missing `early_poll_reason` for a user-requested early check is a
  failure, and one corrected accidental early poll may be pass or soft-fail
  depending on trace impact.
- [ ] LLM judge verdicts are not CI-gating until calibrated: at least five runs
  per candidate scenario, at least four of five matching verdicts, no
  pass/hard-fail oscillation, and applicable score dimensions vary by no more
  than one point.
- [ ] Codex battlefield scenarios use fake time and simulated jobs; they must
  not sleep for real heartbeat intervals or require live GPUs.
- [ ] `job_role` defaulting from `async_mode`.
- [ ] Soft refusal for main launch without positive smoke viability evidence or
  skip reason, even when cadence evidence is present.
- [ ] Representative smoke/preflight mapping and explicit smoke-skip reason.
- [ ] Flexible smoke path: separate smoke script or added smoke mode is accepted
  as ordinary `job_role="smoke"` evidence.
- [ ] Smoke launch without explicit cadence input still heartbeats and uses
  minimum first poll cadence.
- [ ] Successful smoke status emits smoke-to-main guidance.
- [ ] Invalid `smoke_job_id` (missing, cross-repo, wrong-role, unreadable
  lifecycle/outcome) is refused as smoke evidence.
- [ ] Valid successful `smoke_job_id` without cadence evidence is positive
  viability evidence only and uses conservative minimum first cadence.
- [ ] Valid failed, running, or unknown-outcome `smoke_job_id` is recorded as a
  smoke observation but not positive viability evidence, and does not satisfy the
  smoke-or-skip precondition by itself.
- [ ] No-smoke main launch with only `smoke_skip_reason` records the reason and
  uses conservative minimum first cadence.
- [ ] `smoke_cadence_representative=true` allows smoke runtime to contribute to
  cadence basis after successful smoke validation.
- [ ] Interval revision with fresh heartbeat.
- [ ] `update_cadence` owner-state writes are deterministic under interleaving:
  heartbeat versus cadence update, full status versus cadence update when status
  mutates overlapping repo-local cadence fields, rapid consecutive cadence
  updates, and no partial `heartbeat_interval_sec`/`last_heartbeat_at` metadata
  write.
- [ ] `update_cadence` refuses for non-owned reservations, stale policy,
  unhealthy heartbeat manager, missing cadence input, missing/blank reason, and
  failed metadata writes without partially updating interval or heartbeat.
- [ ] Hook injects the structured smoke-or-skip precondition before a main/long
  launch missing positive smoke viability evidence and skip reason.
- [ ] Due reminder includes status-check and cadence-revision guidance.
- [ ] Estimates never bypass process-proof cleanup.

## Phase-Local Gates

- Before Phase 3, pin the exact agent-facing manual guidance for stale
  reservations with null `remote_pid` and unknown launch outcome. The safety
  behavior is already fixed: fail closed unless explicit proof shows no process
  was launched.
- Before changing `kill_gpu_process`, inventory the existing response shape and
  preserve the inspect-then-fingerprint-confirm signaling contract unless a
  specific incompatibility is called out in the Phase 3 or Phase 5 tests.
