# ADR 0004: Multi-Agent GPU Coordination

## Status

Proposed.

## 1. Why This Exists

GPU MCP was built for one agent working in one research repo. That assumption breaks down in a shared lab where multiple researchers (or multiple sessions from the same researcher) run Codex agents simultaneously, all submitting jobs to the same GPU cluster.

The specific problem: two agents can call `check_gpus` within seconds of each other, both see GPU 0 as free, and both launch jobs to it. The result is a collision — out-of-memory errors, corrupted training runs, wasted hours.

This ADR adds a lightweight coordination layer that prevents such collisions without turning GPU MCP into a scheduler, a database, or a daemon.

## 2. What This Is and Is Not

This is a **cooperative reservation registry** — a shared filesystem directory where each reserved GPU is represented by a small metadata file. It is:

- **Cooperative**, not secure. It prevents well-behaved agents from stepping on each other. It does not stop the same Unix user from bypassing it entirely.
- **A reservation list**, not a scheduler. It does not queue jobs, assign GPUs, or manage priorities.
- **Filesystem-based**, not daemon-based. It uses atomic directory creation for locking, avoiding TCP ports, process monitoring, and single points of failure.

It is explicitly **not** a replacement for Slurm, Kubernetes, or a real HPC scheduler. If a lab outgrows the 1–10 agent scale, the correct migration is to a real scheduler, not a more complex MCP-native coordinator.

## 3. The Four Cases That Drive the Design

1. **Multiple repo agents must not double-book one GPU.** Two agents in different repos should never independently select the same GPU.
2. **An agent that lost context must find its own repo's active reservation.** A Codex session may crash or restart. The next session in the same repo should be able to discover what the previous session left running.
3. **A dead agent must not strand a GPU forever if its process is gone.** If an agent dies and its remote GPU job also exits, the GPU should eventually become available again.
4. **MCP must stay lightweight.** No daemon, no database, no queue, no scheduler, no cross-agent service.

## 4. The Decision

Add a shared per-user filesystem registry at `~/gpu-mcp/state/reservations/`.

Each reserved GPU gets one directory named after its canonical host and GPU index (for example, `gpu-a.gpu0/`). Inside the directory is a small `metadata.json` file describing who reserved it, when, and whether the reservation is still alive.

The coordination rule is simple:

> Before choosing a GPU, an agent must look at the registry. Before launching a job, the MCP server must atomically reserve the chosen GPU by creating that GPU's reservation directory. Observer-initiated cleanup may remove a reservation only when the owner's heartbeat is stale and remote inspection proves the original job process is gone. Owner-initiated `finish` is a voluntary surrender path and may remove the reservation immediately after inspection proves the process is already gone.

### 4.1 Registry Location

The registry must be shared across repos. If each repo kept its own registry, agents in different repos could not see each other's reservations, and double-booking would still be possible.

The shared location is:

```
~/gpu-mcp/state/reservations/
```

All MCP server processes for the same Unix user read and write this directory. Different Unix users have separate namespaces naturally.

The server should resolve the registry path, verify that the resolved directory is owned by the current user, and require user-only permissions. Lab home directories and scratch roots are often symlinked, so a symlinked parent path is acceptable after resolution. Symlinks inside the registry itself, including reservation entries and metadata files, must still be rejected. This is not a security boundary — the same Unix user could edit it directly — but it prevents accidental writes through unsafe paths.

### 4.2 Reservation Keys

The directory name is the reservation key, derived from the canonical host identity and GPU index. The canonical host identity is the active policy node name after host validation, not arbitrary user input. For example:

- Policy node `gpu-a` → canonical host `gpu-a`
- GPU index `0` → reservation key `gpu-a.gpu0`

This canonicalization is critical. If one agent uses `gpu-a` and another uses `gpu-a.example.edu`, they could create separate reservation directories for the same physical GPU and collide. Every tool — `check_gpus`, `run_python_on_gpu`, reservation creation, cleanup — must use the same canonical key.

The v1 reservation key format is:

```text
<canonical-host>.gpu<gpu-index>
```

The v1 implementation should reject canonical host names that are not safe as a single directory component.

### 4.3 Atomic Reservation

The reservation is granted by a single atomic operation: creating a directory.

1. Build the reservation key (`gpu-a.gpu0`).
2. Attempt to create `~/gpu-mcp/state/reservations/gpu-a.gpu0/`.
3. If creation succeeds, the caller owns the reservation.
4. If creation fails because the directory already exists, the GPU is reserved by someone else.

Why a directory, not a file? Because two processes can both read a file, both see it is empty, both write their own version, and both proceed to launch. A directory can only be created once; the filesystem itself serializes the race.

If the existing reservation is stale and has been cleaned up, the caller must retry the atomic creation. Cleanup does not transfer ownership; only a fresh successful creation does.

### 4.4 Stable State Roots and IDs

The v1 shared registry root is fixed:

```text
~/gpu-mcp/state/reservations/
```

This matches ADR 0002's trusted user-level state directory. Production behavior should not require a repo-local config knob for this path. Tests may inject an alternate root so they do not touch real user state.

Repo-local managed job records live under:

```text
<repo>/.gpu_mcp_state/jobs/
```

This directory stores current-repo details that do not belong in shared metadata: full script path, arguments, output/log paths, outcome records, and hook reminder state. It should be treated like `.gpu_mcp_runner/` and `.gpu_mcp_logs/`: local operational state, not source code.

`job_id` is an opaque handle generated at launch:

```text
job-<UTC timestamp>-<random suffix>
```

Example: `job-20260528T153012Z-a1b2c3d4`. It is for tool targeting and recovery, not for authorization.

`server_instance_id` identifies one live MCP server process:

```text
server-<local-hostname>-<pid>-<random suffix>
```

Example: `server-login01-42817-a1b2c3d4e5f6`. It is not a secret. Its only purpose is to ensure that a new server process does not accidentally heartbeat or mutate reservations owned by an old process.

## 5. What Gets Stored (and Why)

The metadata file inside each reservation directory contains only what other agents need to avoid collisions and help recovery. The guiding question is: if Repo B looks at Repo A's reservation, what should Repo B legitimately know?

**Repo B must know:** which GPU is taken, by which repo, since when, and whether the reservation is still alive. This prevents collisions.

**Repo B should know:** a rough description of the job. This helps humans understand cluster contention.

**Repo B must not know:** script arguments, full file paths, or output locations. These can contain sensitive data — API tokens, dataset paths, personal information. Same Unix user is not a justification for sharing sensitive command-line content.

Therefore, the shared metadata includes:

| Field | Purpose |
|-------|---------|
| `job_id` | Opaque handle for lifecycle operations |
| `repo` | Which repo reserved this GPU (for recovery) |
| `script_name` | Just the filename, e.g. `train.py` (human-readable, no sensitive data) |
| `host`, `gpu_index` | Self-describing location |
| `owner_user` | Unix account |
| `server_instance_id` | Which MCP server process owns the heartbeat for this reservation |
| `remote_pid`, `remote_start_time`, `remote_boot_id`, `process_fingerprint` | Remote process identity (for inspection). The fingerprint may be a launcher nonce or other opaque non-sensitive value; it must not encode command-line arguments. |
| `reserved_at`, `last_heartbeat_at`, `heartbeat_interval_sec` | Lease timestamps |

Notably absent: `args_preview`, `script_path`, and `output_file`. These belong in repo-local job records, not shared cross-repo state.

If metadata is malformed or manually edited, tools must fail closed for that reservation rather than guessing. A corrupted reservation is treated as occupied until an administrator intervenes.

## 6. The Cleanup Rule

The most important decision in this ADR is when an observer may remove a reservation it does not own. The rule has two parts:

> **Observer-initiated cleanup may remove a reservation only when two independent conditions are met:**
> 1. The owner's heartbeat has gone stale (the owner has not checked in for longer than expected).
> 2. Remote process inspection proves the original GPU job is gone or has a different identity.

For observer-initiated cleanup, both conditions must be true. Neither alone is sufficient. Owner-initiated `finish` is a separate voluntary surrender path described below.

### Why both conditions?

- **Heartbeat alone is not enough.** The agent might have crashed while the remote GPU job keeps running. Freeing the reservation based only on a missing heartbeat would allow another agent to schedule onto a GPU still occupied by the first agent's job.
- **Process proof alone is not enough.** The agent might have deliberately killed its job and is about to retry under the same reservation. The heartbeat is still fresh, so the owner still holds the lease.

### What counts as "process proof"?

Remote inspection compares the stored process identity against the current state of the remote host:

- Does a process still exist at the stored `remote_pid`?
- Does the process owner match `owner_user`?
- Does its start time match?
- Does its opaque process fingerprint or launcher nonce match?
- Does the host's boot ID match?
- Is the process a real live process rather than a zombie?

If any of these differ, the stored PID is either dead, reused by a different process, or from a rebooted host. The original job is gone.

A matching live process keeps the reservation even if it is temporarily off the GPU (for example, doing CPU-side cleanup between training epochs). Absence from `nvidia-smi` alone does not prove a process is gone. A zombie process counts as gone for cleanup purposes; it has exited even if the PID remains in the process table.

### Sub-rules

- Observer-initiated cleanup (e.g., `check_gpus`, `run_python_on_gpu` finding a stale reservation) requires both stale heartbeat and process proof. A fresh heartbeat means the owner is still managing the lease; observers must not override it.
- Owner-initiated `finish` is different: the owner voluntarily surrenders the lease only after process-gone proof. The server inspects the process immediately. If already gone, it cleans up without waiting for staleness. If still alive, `finish` refuses, keeps heartbeating, and tells the agent to call `stop` first if it intends to terminate the job or continue polling `status` if it intends to wait.
- Never free a stale reservation merely because the heartbeat is missing.
- Killing a process and freeing a reservation are separate operations. After a rescue kill, the reservation stays until the next inspection proves the process is gone.
- Cleanup must be atomic and race-safe. Two agents might both discover the same stale reservation at the same time. The protocol must ensure only one cleans it, and neither acts on stale data if the owner refreshes its heartbeat mid-cleanup. The final cleanup step must be guarded so a heartbeat cannot refresh the reservation between the final metadata check and the quarantine rename; this guard must be short-lived and automatically released on process death, not a persistent lock directory.

## 7. Recovery

When an agent loses its `job_id` (for example, after a Codex crash and restart), it needs to find its reservation again. The registry stores the repo path, so the agent can ask: "show me reservations for my repo."

If exactly one reservation matches the current repo, it is the implicit recovery target. If there are zero or multiple, the agent must disambiguate explicitly. The server never guesses.

A new MCP server instance never adopts reservations created by a prior instance. It can inspect and report them, and it can clean them up if they are stale and the process is gone. But it does not take over heartbeat ownership for a still-running old job. If the old process needs to be stopped, the caller uses the managed owner path (if the original session is still alive) or explicitly chooses the fingerprinted rescue-kill path (if the reservation is stale or orphaned).

## 8. What Changes for Tools

- Ordinary GPU runs and smoke probes use managed background jobs. A launch call reserves the GPU, starts the remote process, and returns promptly with a `job_id`; `manage_gpu_job(status)` is how the agent follows progress and retrieves results. Tool timeouts apply to launch/status RPCs, not to total GPU job runtime.
- The managed launcher writes a repo-local outcome record with exit code, signal if known, and end time. If `status` later finds the process gone but no outcome record exists, it reports process-gone-with-unknown-outcome rather than guessing success or failure.
- The launch result must at minimum include `status`, `job_id`, `reservation_key`, `host`, `gpu_index`, `server_instance_id`, process identity summary when known, a current-repo output/log pointer when available, and `next_poll_after`.
- A status result must at minimum include `job_id`, `reservation_key`, computed reservation state, job lifecycle when known, ownership (`owned_by_current_server`), heartbeat age/staleness details, process inspection summary when performed, allowed actions, current-repo output/log pointers when available, and the next suggested poll time.
- **`check_gpus`** now reads the registry before reporting. For reserved GPUs, it reports the reservation state instead of `AVAILABLE`. It may inspect stale reservations and perform observer cleanup when stale heartbeat plus process proof shows the original job is gone, but it does not perform deep inspection when the heartbeat is fresh. `check_gpus` is not an owner-side lifecycle mutation and remains available even if the local heartbeat manager is unhealthy; only owner-side mutations are refused when the heartbeat manager is unhealthy.
- **`run_python_on_gpu`** now atomically reserves the GPU before launching. If the GPU is already reserved, it refuses with a clear message. If launch fails before a remote process exists, it removes the reservation so the GPU is not orphaned.
- **No explicit `release_gpu_reservation` tool.** Releasing a live reservation is dangerous — another agent could schedule onto a GPU still occupied by the old job. Reservations are removed only through owner `finish` after process-gone proof, or through observer cleanup after stale heartbeat plus process-gone proof.
- **`kill_gpu_process`** remains a separate rescue concept. It inspects a specific host/PID, requires fingerprint confirmation, and does not directly remove reservations. Because it does not depend on local lease ownership, it remains available even if the local heartbeat manager is unhealthy. Cleanup follows the normal two-condition rule on the next inspection.
- All operational cluster tools still obey ADR 0002's active-policy rule. If `gpu-mcp.toml` has changed but not been approved and reloaded, normal tools refuse rather than running under ambiguous policy. Policy preview/reload tools are the recovery path.

## 9. Rejected Alternatives

### Persistent TCP Daemon
A central daemon would avoid filesystem races but introduces a single point of failure, requires a network port, and needs monitoring. The filesystem registry achieves the same coordination for our scale without any of this.

### SQLite or Embedded Database
SQLite would provide atomic transactions but adds a schema, a file lock protocol, and a dependency. Per-GPU directory creation is simpler.

### Per-Repo Reservation Files
If each repo had its own registry, agents in different repos could not see each other's reservations. Cross-repo coordination would fail.

### SSH-Based Lock Files on GPU Hosts
Creating lock files on each GPU host via SSH would work without a shared filesystem but adds N round-trips per check and complicates cleanup when an agent crashes.

### Agent-Side Coordination Only
Telling agents to "just check `nvidia-smi` and tolerate collisions" does not solve the problem. At small scale collisions are rare but expensive — OOMs, corrupted runs, wasted hours.

### Full Scheduler with Queuing
Out of scope. ADR 0001 explicitly excludes general HPC scheduler features.

## 10. Consequences

This adds reservation state alongside the approved-policy state under `~/gpu-mcp/state/`. The trust model does not change — this directory was already treated as trusted control state.

The design stays lightweight: no daemon, no database, no new dependencies. The implementation must share process identity logic across launch, cleanup, lifecycle management, and rescue kill paths, which is nontrivial but bounded.

For 1–10 agents in a single lab, this is sufficient. If the lab grows beyond that, migrate to Slurm or Kubernetes, not a more complex MCP-native coordinator.
