# ADR 0004 Implementation Companion: Multi-Agent GPU Coordination

## 1. Introduction

This document is the implementation guide for ADR 0004. It takes the architectural decisions and invariants from the ADR and expands them into concrete guidance for writing code.

If you are reading this to understand *why* the system works the way it does, start with ADR 0004. If you are reading this to *implement* it, you are in the right place.

This companion covers:
- What the metadata file looks like and why each field exists
- What the five reservation states mean and when they appear
- Nine concrete scenarios that every implementation must handle correctly
- How cleanup actually works, step by step
- What the heartbeat manager must provide (the boundary to ADR 0004p)
- How each tool interacts with the registry
- How to inspect remote processes without getting fooled by PID reuse
- How to test all of this

This document will evolve as edge cases are discovered in implementation.

## 2. The Metadata File

Each reservation directory contains one file: `metadata.json`. It is diagnostic state, not the lock. The lock is the directory itself.

### Why a JSON file inside a directory?

The directory is the atomic lock — it can only be created once. The JSON file inside holds the details. Writers update the JSON file using temp-file-plus-`os.replace` so readers never see a partially written file. But the JSON file is never the authority for whether a GPU is reserved; only the directory's existence matters.

### What fields exist (and why)

```json
{
  "job_id": "job-20260526-abcdef",
  "reservation_key": "gpu-a.gpu0",
  "host": "gpu-a",
  "gpu_index": 0,
  "repo": "/net/levsha/scratch2/tingran/repo-a",
  "script_name": "train.py",
  "owner_user": "tingran",
  "server_instance_id": "server-login01-12345-abcdef123456",
  "remote_pid": 23456,
  "remote_start_time": "2026-05-26T12:00:10Z",
  "remote_boot_id": "boot-uuid-or-equivalent",
  "process_fingerprint": "gpu-mcp-process:abc123",
  "reserved_at": "2026-05-26T12:00:00Z",
  "last_heartbeat_at": "2026-05-26T12:10:00Z",
  "heartbeat_interval_sec": 600
}
```

| Field | Why it exists |
|-------|---------------|
| `job_id` | The handle the agent uses to ask about its job later. Opaque to the registry. |
| `reservation_key`, `host`, `gpu_index` | Self-describing location. The key is also the directory name, but the file should be readable on its own. |
| `repo` | Recovery. When an agent loses its `job_id`, it searches by repo. |
| `script_name` | Human-readable label. "What job is running?" — "train.py." No path, no arguments, so no sensitive data leaks. |
| `owner_user` | Sanity check. Should match the current Unix user. |
| `server_instance_id` | Owner identity. The MCP server that created this reservation is the only one that may heartbeat it. A new server process gets a new ID and never adopts old reservations. Format: `server-<local-hostname>-<pid>-<random suffix>`. The random suffix is the uniqueness source; hostname and PID are diagnostic. |
| `remote_pid`, `remote_start_time`, `remote_boot_id` | Process identity facts. Used to answer: "Is the original job still running?" |
| `process_fingerprint` | Opaque non-secret process identity, such as a launcher nonce or one-way hash. It must not contain command-line arguments. Used by the rescue kill tool to confirm it is targeting the right process. |
| `reserved_at`, `last_heartbeat_at` | Timestamps. `last_heartbeat_at` tells observers when the owner last checked in. |
| `heartbeat_interval_sec` | How often the owner promised to heartbeat. Observers use this to compute when the reservation becomes stale. |

### Fields that were deliberately removed

- `args_preview`: Command-line arguments can contain API keys, dataset paths, or prompt-injection text. Not safe for shared state.
- `script_path`: Full paths are repo-local context. Other agents do not need them.
- `output_file`: Output locations are private to the repo.

If an agent needs the full command line or output path, it resolves the `job_id` through its repo-local job tracking, not the shared registry.

If metadata is malformed, unreadable, or manually edited, tools must fail closed for that reservation rather than guessing. Treat the GPU as occupied and surface an error. Do not attempt to reconstruct or repair corrupted metadata automatically.

## 3. The Five States You Will See

These are diagnostic labels that tools compute on demand and report to agents. They are **not persisted in metadata** — they are derived from `last_heartbeat_at` and (when needed) remote process inspection. A tool computes state when asked; it does not write state back to the registry.

Think of these like weather reports: "sunny" or "rainy" describes what you see right now, but the decision to carry an umbrella is a separate rule.

Fresh reservations do not always need remote inspection. A broad `check_gpus` call may safely report a fresh reservation as reserved without distinguishing running from idle. The detailed `RESERVED_RUNNING` and `RESERVED_IDLE` labels require process information and are normally produced by `manage_gpu_job(status)`, by targeted inspection, or by a recent cached inspection result.

### RESERVED_RUNNING
The heartbeat is fresh, and remote inspection confirms the original process is alive and on the GPU. This is normal operation.

### RESERVED_IDLE
The heartbeat is fresh, but the process is either gone or no longer on the GPU. The owner still holds the lease — perhaps it stopped the job and is about to retry, or the process is doing CPU-side cleanup. Other agents must not take the GPU.

### STALE_RESERVED
The heartbeat is stale, but remote inspection proves the original process is still alive. The owner has probably died, but the remote job continues. The GPU stays reserved. Another agent may use the rescue kill tool if needed.

### UNKNOWN_RESERVED
The heartbeat is stale, and the implementation cannot inspect the remote host (SSH down, host unreachable, `nvidia-smi` failing). The system does not know whether the job is still running, so the reservation stays. A later tool call may succeed in inspection.

### AVAILABLE
No reservation exists, or a stale reservation was removed after process inspection proved the original job is gone. The GPU may be reserved by a new agent.

## 4. Nine Scenarios Every Implementation Must Handle

These scenarios cover the significant combinations of heartbeat freshness and remote process state. For each, we describe the situation, what the user sees, and what the implementation must do.

### Scenario 1: Normal operation
An agent launched a job. The heartbeat is recent. The remote process is alive and on the GPU.

- **`check_gpus`**: Reports a fresh reservation with job name and heartbeat age. It does not need to inspect the process merely to refine the label.
- **`manage_gpu_job(status)`**: Returns running state with process details.
- **What the agent should do**: Poll status until the job finishes.

### Scenario 2: Between retry attempts
The agent stopped its job (bad parameters, runaway loop) and is preparing to retry. The process is gone, but the heartbeat is still fresh.

- **`check_gpus`**: Reports a fresh reservation, not availability.
- **`manage_gpu_job(status)`**: May report `RESERVED_IDLE` after inspection.
- **`manage_gpu_job(retry)`**: Allows `retry` to relaunch under the same reservation after confirming the prior process is gone.
- **What the agent should do**: Fix parameters, then retry. Other agents must not schedule here.

### Scenario 3: Agent died, job still running
The local agent or MCP server crashed. The heartbeat stopped. But the remote process is still alive on the GPU.

- **`check_gpus`**: Reports `STALE_RESERVED`.
- **`manage_gpu_job`**: Read-only `status` is allowed. `stop` and `retry` are refused because the current server does not own the reservation.
- **What another agent should do**: Treat the GPU as occupied. Choose another GPU or wait. Use `kill_gpu_process` only as an explicit, fingerprint-gated rescue action, not as a normal scheduling shortcut. The caller may be an autonomous agent, but it must intentionally choose the rescue path for the specific target.

### Scenario 4: Agent died, job also gone
The agent crashed, and the remote process exited (or was killed).

- **`check_gpus`**: Reports `AVAILABLE` after cleanup.
- **Cleanup**: Stale heartbeat + process proof = reservation removed.
- **What another agent should do**: The GPU is free for a new reservation.

### Scenario 5: Cannot reach the host
The heartbeat is stale, but SSH to the GPU host fails.

- **`check_gpus`**: Reports `UNKNOWN_RESERVED` with the last error.
- **Cleanup**: None. Without process proof, the reservation stays.
- **What any agent should do**: Check again later. If the host is permanently down, an admin must manually quarantine.

### Scenario 6: PID reuse
A process exists at the stored PID, but its start time, boot ID, or fingerprint does not match. The original job is gone; a different process has reused the PID.

- **`check_gpus`**: Reports `AVAILABLE` after cleanup.
- **Cleanup**: Stale heartbeat + identity mismatch = original job gone.

### Scenario 7: Alive but temporarily off the GPU
The process exists in `ps` but `nvidia-smi` does not show it on the reserved GPU.

- **If heartbeat is fresh**: broad `check_gpus` still reports the GPU as reserved; targeted status may report `RESERVED_IDLE` if inspection shows the process is off-GPU or gone. The owner may retry only after confirming the previous process is gone.
- **If heartbeat is stale**: `STALE_RESERVED`. The matching process is still alive, so the reservation stays. Do not free.
- **Why**: The process may be between training epochs, doing CPU cleanup, or temporarily between GPU phases.

### Scenario 8: Owner stops its own job
The agent calls `manage_gpu_job(action="stop")`. The remote process receives SIGTERM. The agent keeps heartbeating.

- **`check_gpus`**: Reports a fresh reservation, not availability.
- **`manage_gpu_job`**: `stop` succeeds. Status may report `RESERVED_IDLE` after process-gone proof. `retry` is allowed only after that proof. `finish` is allowed.
- **What the agent should do**: Either retry with fixed parameters or finish to abandon the reservation. Stopping the process is not the same as releasing the reservation.

### Scenario 9: Rescue kill by another agent
After a caller explicitly chooses to terminate a stale matching process as a rescue action, it may call `kill_gpu_process` with matching fingerprint. This can be an autonomous agent action; the important property is explicit target selection plus fingerprint confirmation, not human approval.

- **`kill_gpu_process`**: Inspects the host/PID, confirms fingerprint, sends signal.
- **Reservation**: Stays `STALE_RESERVED`. The kill tool does not remove reservations.
- **Cleanup**: After the process exits, the next inspection sees process gone + stale heartbeat and cleans up.
- **What the agent should do**: After killing, poll until the GPU becomes `AVAILABLE`.

### The failure this design prevents

The most dangerous failure mode is: the local agent dies, the remote GPU job keeps running, and another agent sees the GPU as available because the heartbeat stopped. If that second agent schedules onto the same GPU, both jobs collide — out of memory, corrupted results, wasted hours.

This is why the two-condition rule exists. A missing heartbeat is only a reason to inspect; it is never proof that the GPU is free. Only process inspection can confirm the original job is actually gone.

## 5. How Cleanup Actually Works

Cleanup is the most delicate part of the implementation. It must satisfy the invariant (never free unless stale + process gone) while handling races between multiple cleaners and late heartbeats.

### The protocol

1. **Read the reservation metadata.**
2. **Check staleness.** If the heartbeat is not stale, stop. Fresh reservations are never cleaned up.
3. **Inspect the remote process.** SSH to the host, check `ps`, check `nvidia-smi`. If the process is alive and matches, stop. If inspection fails (host unreachable), stop.
4. **Re-read the metadata.** This is critical. Between steps 1 and 3, the owning agent might have refreshed its heartbeat.
5. **Confirm still stale.** If the heartbeat was refreshed while you were inspecting, stop. If the process identity facts changed while you were inspecting, stop.
6. **Acquire the cleanup finalization guard.** This must be a short-lived advisory file lock or equivalent OS lock shared with heartbeat writes for the same reservation. It is not a persistent `.cleanup-lock/` directory or marker.
7. **Re-read and confirm again under the guard.** If the heartbeat refreshed or metadata changed before the guard was acquired, release the guard and stop.
8. **Rename the reservation directory to quarantine.** Use an atomic rename to a path like `~/gpu-mcp/state/reservations/.quarantine/gpu-a.gpu0.<timestamp>`. Rename is atomic on the same filesystem and avoids the race where one cleaner removes a directory while another is reading it.
9. **Done.** The original reservation key no longer exists. A new agent may now create it.

### Why re-read?

Consider: Agent A's heartbeat is stale. Agent B starts cleanup. While B is SSHing to the host to check the process, Agent A's heartbeat thread (which was merely delayed, not dead) finally writes a fresh heartbeat. Without the re-read, B would clean a reservation whose owner just checked in. Without the finalization guard, a heartbeat could still refresh the reservation between the final re-read and the quarantine rename.

### Why rename instead of delete?

- Atomic on the same filesystem.
- Preserves metadata for debugging.
- Avoids races with concurrent readers.
- Quarantined directories can be garbage-collected later by a bounded sweep or by an admin.

### Two cleaners race

If two agents both discover the same stale reservation:
- Both read metadata.
- Both inspect the process.
- Both acquire the finalization guard before the final re-read and rename.
- Only one guarded rename succeeds. The other sees that the source no longer exists and aborts.
- This is safe. The guard is short-lived and only protects the final heartbeat-vs-cleanup window; the atomic rename remains the operation that makes the reservation key disappear.

### Stale threshold

A reservation is stale when:

```
now - last_heartbeat_at > heartbeat_interval_sec * STALE_MULTIPLIER
```

ADR 0004p sets `STALE_MULTIPLIER = 3` for v1. This means an owner must miss three expected heartbeats before observers treat the reservation as stale enough to inspect for cleanup.

If `heartbeat_interval_sec` is missing or malformed, treat the reservation as stale (fail closed) but still require process proof before cleanup.

## 6. What the Heartbeat Manager Must Provide

The heartbeat manager is defined in ADR 0004p. From the reservation protocol's perspective, it is a black box that must provide four things:

1. **Periodic heartbeat writes.** For each reservation owned by the current server, write `last_heartbeat_at` at a cadence derived from the reservation's `heartbeat_interval_sec`.
2. **Stale threshold computation.** Given a reservation's `heartbeat_interval_sec`, return the threshold at which it becomes stale.
3. **Health status.** Expose a lightweight `is_healthy()` check that the server can poll. If the heartbeat manager is unhealthy (thread died, filesystem writes failing), the server must refuse owner-side mutations like launch, retry, stop, and finish. Read-only tools and the fingerprint-gated rescue kill path remain available.
4. **Owner identity.** Tag heartbeats with the current `server_instance_id` so other servers know which instance wrote them.

The reservation protocol does not care how the heartbeat manager is threaded, whether it batches writes, or how it recovers from errors. Those are 0004p's concerns.

## 7. How Each Tool Uses the Registry

Before any operational tool in this section runs, validate that the active in-memory policy still matches the approved `gpu-mcp.toml` state from ADR 0002. If the repo policy file has changed but has not been approved and reloaded, refuse normal cluster operations. This includes `check_gpus`, `run_python_on_gpu`, `manage_gpu_job`, `list_gpu_reservations`, and `kill_gpu_process`. Policy preview/reload tools are the recovery path.

Policy freshness is separate from heartbeat-manager health. A stale policy blocks normal operational tools because the server no longer knows which hosts, roots, and timeouts are approved. An unhealthy heartbeat manager blocks owner-side lifecycle mutations, while read-only diagnostics and the fingerprint-gated rescue kill path may still work under the already-approved active policy.

### check_gpus

Before reporting GPU availability, read the registry. For each GPU:
- If a reservation exists and the heartbeat is fresh, report the reservation state without remote inspection.
- If a reservation exists and the heartbeat is stale, perform bounded inspection to distinguish `STALE_RESERVED`, `UNKNOWN_RESERVED`, and `AVAILABLE`.
- If a stale reservation is skipped because the inspection budget is exhausted, keep it unavailable and report it as stale-uninspected or unknown-reserved. Budget limits cleanup work, not safety.
- Batch inspection by host rather than opening one SSH session per GPU.
- Use per-host timeouts and an overall budget. If the budget is exhausted, report the current state and let a later call retry.

`check_gpus` is read-only. It works even if the local heartbeat manager is unhealthy, because it is only reading the registry and optionally inspecting remote processes. It does not require the local server to own any reservations.

### run_python_on_gpu

Launch is a managed-background-job operation. The tool reserves the GPU, starts the remote process, records process identity, and returns promptly with a `job_id` and `reservation_key`. It should not block until the GPU job completes. If a legacy `async_mode=False` argument is still accepted during migration, treat it as compatibility input and still return the managed job handle; do not preserve a second long-running inline execution path.

Before launching:
1. Check if the target GPU has an existing reservation.
2. If reserved and not cleanable, refuse with a clear message: "GPU 0 on gpu-a is reserved by your repo's train.py job" or "GPU 0 on gpu-a is reserved by another repo."
3. If reserved but stale + process proof allows cleanup, run the cleanup protocol, then retry atomic reservation.
4. If not reserved, atomically create the reservation directory.
5. Launch the job.
6. Update metadata with `remote_pid` and process identity.

If launch fails before a remote process exists, remove the reservation. If the client disconnects after launch, leave the reservation in place for recovery.

The managed launcher must arrange for a repo-local outcome record when the remote command exits. The outcome record should include exit code when available, terminating signal when known, and end time. This record belongs in the repo-local job state, not shared reservation metadata. `manage_gpu_job(status)` uses it to distinguish success from failure; if inspection shows the process is gone but the outcome record is missing, malformed, or unreadable, status reports process-gone-with-unknown-outcome rather than guessing.

### manage_gpu_job

The umbrella lifecycle tool: status, stop, retry, finish.

**Target resolution** (strict order):
1. `job_id` if provided.
2. `reservation_key` if provided.
3. Repo-scoped recovery: if exactly one current-repo reservation exists, use it.
4. Otherwise, return candidates or a no-target result.

Repo-scoped recovery is a lost-context convenience, not a third exact identifier. If multiple current-repo reservations exist, return candidates and require the caller to choose. Do not guess based on recency, host, GPU index, state, or script name.

Resolving a target does not grant lifecycle authority. If the resolved reservation is owned by the current live MCP server instance, owner actions may proceed subject to the normal health checks. If it is owned by another live or prior server instance, `status` may report it, but `stop`, `retry`, and `finish` must refuse. Rescue kill remains a separate fingerprint-gated path and does not imply lease ownership.

Candidate entries should be useful but sanitized. Include `job_id`, `reservation_key`, `host`, `gpu_index`, `script_name`, computed state, `reserved_at`, heartbeat age, owning `server_instance_id`, whether the current server owns it, allowed actions, and the last inspection summary if available. Do not include `args_preview`. Do not include `script_path` or `output_file` in shared cross-repo candidate output; current-repo status may resolve repo-local log/output pointers through the local job record.

**Actions:**
- `status`: Read-only and allowed for a resolved target after the policy freshness check. Reports state, heartbeat age, process identity, last inspection. Works even if the local heartbeat manager is unhealthy.
- `stop`: Only if the current server owns the reservation. Sends SIGTERM but does not remove the reservation. While the process is still exiting, broad availability checks still report the GPU as reserved; owner status may report that stop was requested and whether the process is still alive or already gone. Status may report `RESERVED_IDLE` only after inspection proves the process is gone. Refused if the heartbeat manager is unhealthy.
- `retry`: Only if owned. Before relaunching under the same reservation, inspect the current recorded process. If the matching process is still alive, refuse retry for this reservation because retry must not create a second process on the same GPU. The agent may keep polling this job, explicitly stop it if it wants to replace this attempt, or launch a separate new job on another available GPU through normal reservation acquisition. If the process is gone or was already stopped, relaunch under the same reservation and update process identity. Refused if the heartbeat manager is unhealthy.
- `finish`: Only if owned. The owner is done. Stop heartbeating and inspect the process immediately. If already gone, clean up the reservation now. If still alive, the reservation remains occupied; after the heartbeat ages past the stale threshold, observers may report `STALE_RESERVED` while the process lives. Cleanup will happen later when the process exits and the next inspection finds it gone. Refused if the heartbeat manager is unhealthy.

### list_gpu_reservations

List active reservations. Default scope is the current repo (`"mine"`) for recovery. `"all"` is available for cluster-wide diagnostics.

May perform bounded inspection of stale/unknown reservations when asked for fresh status. Plain listing should be fast.

Like `check_gpus`, this is read-only and works even if the local heartbeat manager is unhealthy.

### kill_gpu_process

Rescue tool, not the normal lifecycle API.

- Inspect the specific host/PID.
- Require matching `process_fingerprint` before signaling.
- Remind the agent that killing is disruptive, that the target must be one inspected host/PID, and that this is a rescue path rather than normal lifecycle management.
- Remain available even if the local heartbeat manager is unhealthy; heartbeat health governs lease ownership, not fingerprint-gated rescue actions.
- Do NOT remove any reservation. Cleanup follows the normal stale + process proof rule on the next inspection.

### Tool output and Codex-facing hooks

Tool output and any Codex-facing hook should teach the same operating discipline:

- After launch or retry, keep a live obligation to poll `manage_gpu_job(status)` until the job reaches a terminal state. Do not proceed with work that depends on the job's outputs while the job is still running or retrying, or while reservation diagnostics are unclear because inspection failed. This does not require the agent to idle: it may work on independent tasks that do not depend on the GPU job's outputs, as long as it preserves the obligation to return to the job at the suggested time. If no independent work is available, waiting idly and polling is acceptable behavior.
- When supported by the client, a user-global GPU MCP `PreToolUse` hook may
  surface due or overdue GPU-job reminders as model-visible context. The hook
  scopes itself to the current repo by walking upward from the hook working
  directory to the nearest `gpu-mcp.toml`; if none is found, it exits quietly.
  When a policy is found, it reads only that repo's job state and hook-owned
  advisory state, and must not emit reminders for another repo's jobs. This hook
  is best-effort and non-blocking: it must not inspect remote processes, call
  MCP tools, or become part of the reservation safety boundary.
- Status is the result-retrieval path. For short smoke probes, status may include a bounded stdout/stderr tail or a pointer to an approved output log. Long-running jobs should return handles, status summaries, and log pointers rather than keeping the launch RPC open.
- Tool timeouts apply to launch/status RPCs and inspection calls, not to total GPU job runtime.
- If the heartbeat manager is unhealthy, tool output should explain that the server cannot safely manage its leases. The agent should pause owner-side lifecycle actions (`launch`, `retry`, `stop`, `finish`) and avoid output-dependent work until heartbeat health recovers. Read-only diagnostics and fingerprint-gated rescue kill remain governed by the rules above.
- The rescue kill reminder is behavioral friction, not a human approval gate and not an ownership boundary. An autonomous agent may use it when it explicitly chooses the rescue path for a specific inspected target and the fingerprint matches.

## 8. Process Identity: How to Not Get Fooled

The same `inspect_remote_process` logic must be used by launch, cleanup, status, and kill. Different tools must not disagree about whether a PID is the original job.

### Facts to check

For a stored `remote_pid` on a remote host:
1. Does a process exist at that PID?
2. Does the process owner match `owner_user`?
3. Does the start time match `remote_start_time` (within tolerance)?
4. Does the host's boot ID match `remote_boot_id`?
5. Does the opaque process fingerprint or launcher nonce match `process_fingerprint`?
6. Is the process attached to the reserved GPU (per `nvidia-smi`)?

### Decision rules

| Situation | Conclusion |
|-----------|------------|
| PID exists, all identity facts match | Original job, still running |
| PID exists, off GPU | Original job, still running (may reacquire GPU) |
| PID does not exist | Original job is gone |
| PID exists, start time differs | PID reused by different process; original is gone |
| PID exists, boot ID differs | Host rebooted; original is gone |
| PID exists, fingerprint differs | Different process; original is gone |
| PID exists, owner differs | Unexpected; treat as different process |
| PID exists, `ps` state is `Z` (zombie) | Original job is gone (reaped but not yet collected) |

### Why zombies matter

A zombie process retains its PID in the process table. Without special handling, identity checks would match PID, start time, and fingerprint — keeping the reservation blocked until init reaps the zombie. This could strand a GPU indefinitely if the parent process is stuck.

The fix: if `ps` shows the process state as `Z` (zombie), treat it as gone for cleanup purposes. The job has exited; it is merely waiting for the parent to collect the status.

### nvidia-smi is a diagnostic, not a gate

`nvidia-smi` answers "is this process on the GPU right now?" It does not answer "is the original job gone?"

A process may be off the GPU because:
- It is doing CPU-side cleanup after `torch.cuda.empty_cache()`.
- It is between training epochs.
- It is stuck after a CUDA error.

Always check `ps` before concluding a process is gone. Absence from `nvidia-smi` alone is never proof of completion.

## 9. Lazy Orphan Reconciliation

The server must not perform a mandatory cluster sweep at startup. Startup should initialize local identity, validate control state, start the heartbeat manager, and begin serving tools. SSH sweeping at startup would make every restart slow and failure-prone.

Cleanup happens demand-driven, during tool calls:
- `check_gpus` inspects stale reservations while preparing its report.
- `run_python_on_gpu` inspects the specific reservation blocking acquisition.
- `list_gpu_reservations` inspects current-repo stale reservations for recovery.
- `manage_gpu_job` inspects the targeted job.

Each inspection is bounded:

| Limit | Suggested value | Purpose |
|-------|-----------------|---------|
| Per-host SSH timeout | 10 seconds | Avoid hanging on down hosts |
| Per-host inspection budget | 2 GPUs | Limit work per host per call |
| Overall tool inspection budget | 5 reservations | Prevent any single call from becoming a full sweep |
| `check_gpus` total timeout | 30 seconds | Keep frequent checks fast |

If inspection is incomplete, report the current state and let a later call retry. No background reconciler thread is required.

`UNKNOWN_RESERVED` is not permanent. When a down host comes back, a later tool call may inspect and clean the reservation if the process is gone.

## 10. Testing

### Unit-testable (no GPU hosts needed)

- Atomic reservation: mock filesystem, verify only one `mkdir` succeeds.
- Target resolution: mock registry, verify `job_id` → `reservation_key` → repo-scoped recovery order.
- Stale threshold: verify formula with mocked timestamps.
- Cleanup races: mock metadata and mock process inspector, verify re-read aborts when heartbeat refreshes.
- Health checks: verify server refuses owner actions when heartbeat manager reports unhealthy.
- Launch failure path: verify reservation is removed if launch fails before remote process exists.
- Disconnect path: verify reservation stays if client disconnects after launch.

### Requires real GPU hosts or realistic SSH mocks

- Process identity inspection with real `ps` and `nvidia-smi`.
- SSH timeout behavior on unreachable hosts.
- Rescue kill with actual signal delivery.
- Full scenario catalog (scenarios 1–9) with controlled process lifecycles.

### Scenario-to-test mapping

| Scenario | Test | What to verify |
|----------|------|----------------|
| 1 | `test_normal_running` | Fresh heartbeat keeps broad `check_gpus` reserved; targeted status with inspection can report `RESERVED_RUNNING` |
| 2 | `test_between_retries` | Fresh heartbeat + process gone stays reserved; targeted status can report `RESERVED_IDLE`, owner can retry |
| 3 | `test_agent_dead_job_alive` | Stale heartbeat + alive process → `STALE_RESERVED`, no cleanup |
| 4 | `test_agent_dead_job_gone` | Stale heartbeat + process gone → cleanup → `AVAILABLE` |
| 5 | `test_host_unreachable` | Stale heartbeat + SSH failure → `UNKNOWN_RESERVED`, no cleanup |
| 6 | `test_pid_reuse` | Stale heartbeat + PID reused → cleanup → `AVAILABLE` |
| 7 | `test_off_gpu` | Fresh stays reserved; targeted inspection may report idle/off-GPU; stale matching process → `STALE_RESERVED` |
| 8 | `test_owner_stop` | Stop succeeds, reservation stays occupied; targeted status can report `RESERVED_IDLE`, retry allowed after process-gone proof |
| 9 | `test_rescue_kill` | Kill with fingerprint match, deferred cleanup |

### Additional tests

| Test | What to verify |
|------|----------------|
| `test_atomic_no_double_book` | Two concurrent `mkdir` attempts; only one succeeds |
| `test_cleanup_race_re_read` | Heartbeat refreshes between read and cleanup; aborts |
| `test_cleanup_race_two_cleaners` | Both see stale; one renames, other aborts cleanly |
| `test_repo_recovery_one` | Exactly one current-repo reservation → implicit target |
| `test_repo_recovery_zero` | Zero current-repo reservations → no-target |
| `test_repo_recovery_multiple` | Multiple current-repo reservations → return candidates |
| `test_unhealthy_refuse_launch` | Heartbeat manager unhealthy → `run_python_on_gpu` refuses |
| `test_retry_refuses_live_process` | `retry` refuses when the matching original process is still alive |
| `test_check_gpus_budget` | Too many stale reservations → report without hanging |
| `test_zombie_treated_as_gone` | Zombie process → treated as gone for cleanup |
| `test_finish_immediate_cleanup` | `finish` + process already gone → immediate cleanup |
