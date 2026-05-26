# ADR 0004: Multi-Agent GPU Coordination

GPU MCP currently assumes one AI agent per research repo, and one active MCP server
process per agent. In a shared lab filesystem, multiple agents (each in their own
repo) may simultaneously submit jobs to the same GPU cluster. Without coordination,
two agents can call `check_gpus`, see the same GPU as available, and both submit
to it.

This ADR defines a lightweight coordination mechanism that prevents GPU collisions
without introducing a persistent daemon, a database, or a general scheduler.

## Status

Proposed.

## Context

The existing design (ADR 0001) explicitly rejects adding a database or daemon for
same-user multi-agent coordination. That rejection was correct for the initial
single-agent scope. The user has since clarified the operational reality:

- One agent per repo. Repos are isolated by path and workflow.
- Multiple repos may be active simultaneously under the same Unix account.
- All repos share the same GPU cluster hosts via SSH.
- The filesystem is shared (NFS/Lustre or equivalent), typical of academic labs.
- Target scale is small: 1–10 simultaneous agents, not high-throughput HPC.

The only resource contention across agents is **GPU device selection**. File-level
contention within a repo is the agent's own responsibility; cross-repo file
contention is negligible because repos are isolated.

The user already built `saunasub`, a lightweight Python job scheduler with a
persistent TCP daemon (`SaunaMaster`) that solves this exact problem. Saunasub
uses a central daemon to serialize job searches, maintain `Taken_targets`, and
prevent collisions. However, saunasub's daemon model carries operational baggage
that conflicts with MCP's design philosophy:

- Open TCP port (6000) exposed to port scanning and authentication probes.
- Hardcoded shared authkey (`b"secret password"`) as the only security boundary.
- Single point of failure: daemon crash loses all job tracking.
- Host key verification disabled (`AutoAddPolicy`) during SSH key deployment.
- Hardcoded site-specific configuration baked into library source.

MCP should not adopt saunasub's daemon architecture. MCP should borrow the
**coordination concept** (exclude lists, job tracking, reservation state) but
implement it through the **shared filesystem** that the lab already has, keeping
MCP stateless and lightweight.

## Decision

Add a filesystem-based GPU reservation registry.

The registry is a small JSON file on the shared filesystem that records which
`(host, gpu_index)` pairs are reserved by which active MCP jobs. It provides
collision avoidance without a daemon, without a database, and without requiring
MCP server processes to communicate directly.

The coordination rule is:

> Before choosing a GPU, an agent must observe the reservation registry. Before
> launching a job, the MCP server must atomically reserve the chosen GPU. A
> reservation without a corresponding running process expires automatically.

This is not a scheduler. It does not queue jobs, auto-assign GPUs, or manage
priorities. It only prevents two agents from independently selecting the same GPU
based on a stale `check_gpus` snapshot.

### Registry Location

The registry lives beside the staged runner directory:

```text
$REPO/.gpu_mcp_reservations/active.json
```

Wait — this is wrong. The registry must be **shared across repos**, not per-repo.
If each repo has its own registry, agents in different repos cannot see each
other's reservations.

The correct location is a user-level shared path:

```text
~/.gpu-mcp/state/reservations.json
```

This path is already the trusted control-state directory (see ADR 0002). All MCP
server processes for the same Unix user read and write this file. Different Unix
users have separate namespaces naturally; we do not try to coordinate across users.

### Registry Shape

```json
{
  "schema_version": 1,
  "reservations": [
    {
      "host": "gpu-a",
      "gpu_index": 0,
      "repo": "/net/levsha/scratch2/tingran/repo-a",
      "pid": 12345,
      "reserved_at": "2026-05-26T12:00:00Z",
      "expires_at": "2026-05-26T13:00:00Z"
    }
  ]
}
```

Fields:

- `host`: Short hostname (same namespace as `gpu-mcp.toml` nodes).
- `gpu_index`: Integer GPU device index.
- `repo`: Absolute path to the reserving agent's repo. For debugging and
  accountability, not for enforcement.
- `pid`: OS process ID of the launched job on the remote host. Populated after
  async launch succeeds; null for sync jobs that have not yet returned.
- `reserved_at`: ISO timestamp of reservation creation.
- `expires_at`: ISO timestamp after which the reservation is stale.

The `repo` field is diagnostic. The registry does not enforce that a reservation
was created by the same repo whose server is reading it. All reservations for
the same Unix user are visible to all MCP servers for that user. The agent is
expected to tolerate reservations from other repos gracefully.

### Atomic Reservation

Reservations must be written atomically to prevent two MCP server processes from
simultaneously reading an empty slot and both claiming it.

The implementation should:

1. Read the current registry.
2. Check if `(host, gpu_index)` is already reserved by an unexpired entry.
3. If free, append a new reservation with `expires_at = now + TTL`.
4. Write the updated registry to a temporary file in `~/.gpu-mcp/state/`.
5. Atomically replace `reservations.json` with `os.replace`.
6. If the replace succeeds, the reservation is held. If another process won the
   race, read again and retry or fail.

This is not a distributed lock protocol. It is a best-effort atomic file update
sufficient for 1–10 agents on a shared filesystem. The race window is the
read-modify-write cycle; on a local or NFS filesystem with reasonable coherence,
this window is small enough for the target scale.

### TTL and Cleanup

Reservations expire automatically. The default TTL should be long enough to cover
most GPU jobs but short enough that a crashed agent does not block a GPU forever.

Suggested default: **30 minutes**.

The MCP server should clean expired entries on every registry read. No separate
garbage-collection process is needed.

For sync jobs, the reservation is released when the job returns (success or
failure). For async jobs, the reservation is released when:
- the agent explicitly calls `release_gpu_reservation(host, gpu_index)`;
- the reservation TTL expires; or
- a new `check_gpus` call observes the reservation is expired and removes it.

### Enhanced `check_gpus`

`check_gpus` should read the reservation registry before reporting availability.

For each GPU, the tool should:

1. Query `nvidia-smi` for utilization and memory (existing behavior).
2. Check the registry for an unexpired reservation on that `(host, gpu_index)`.
3. If reserved, mark the GPU as `RESERVED` instead of `AVAILABLE` or `BUSY`.
4. Include the reservation age (minutes since `reserved_at`) in the output.

The agent sees something like:

```text
GPU 0 | NVIDIA A100 | util_avg=5.0% | mem=1024/40960 MiB | RESERVED (12 min)
GPU 1 | NVIDIA A100 | util_avg=45.0% | mem=20480/40960 MiB | BUSY
GPU 2 | NVIDIA A100 | util_avg=2.0% | mem=512/40960 MiB | AVAILABLE
```

An agent should prefer `AVAILABLE` over `RESERVED`. If all suitable GPUs are
reserved, the agent should wait and `check_gpus` again rather than overriding a
reservation.

### Reservation in `run_python_on_gpu`

When an agent calls `run_python_on_gpu(host, gpu_index, ...)`, the server should:

1. Validate host and GPU index against policy (existing behavior).
2. Read the registry and check for an unexpired reservation on `(host, gpu_index)`.
3. If reserved by another repo, refuse with a clear message:
   ```text
   GPU 0 on gpu-a is reserved by /net/levsha/scratch2/tingran/repo-b
   (reserved 5 minutes ago, expires in 25 minutes).
   Call check_gpus to find an available GPU.
   ```
4. If not reserved, atomically write a reservation entry.
5. Launch the job (sync or async).
6. For async jobs, update the reservation with the remote PID after launch.
7. For sync jobs, remove the reservation when the job completes.

If the launch fails (SSH error, invalid script, etc.), the server must still
remove the reservation so the GPU is not orphaned.

### Explicit Release Tool

A new MCP tool:

```python
@mcp.tool()
def release_gpu_reservation(host: str, gpu_index: int):
    """Release a previously reserved GPU."""
```

This allows an agent to free a GPU before the TTL expires, e.g., after killing
an async job with `kill_gpu_process`.

Calling `release_gpu_reservation` on a reservation held by another repo should
succeed silently or warn. The design does not treat reservations as strong
ownership locks; they are advisory coordination hints.

### Reservation Listing Tool (Optional)

A diagnostic tool:

```python
@mcp.tool()
def list_gpu_reservations():
    """List all active GPU reservations across repos."""
```

This helps an agent understand cluster contention without parsing `check_gpus`
output. It returns the registry contents as structured JSON.

## Rejected Alternatives

### Persistent TCP Daemon (Saunasub Model)

Rejected. A daemon like `SaunaMaster` provides true serialization and avoids
filesystem races, but it introduces a single point of failure, requires
monitoring/restart logic, needs a network port, and creates a security boundary
that MCP's stateless model deliberately avoids. The filesystem registry achieves
sufficient coordination for the target scale with none of the operational burden.

### SQLite or Embedded Database

Rejected. SQLite would provide atomic transactions, but it adds a schema, a file
lock protocol, and a dependency. For fewer than 100 reservation entries, JSON
with atomic file replacement is simpler and sufficient.

### Per-Repo Reservation Files

Rejected. If each repo maintains its own registry (e.g., under
`$REPO/.gpu_mcp_reservations/`), agents in different repos cannot see each
other's reservations. Cross-repo coordination is the entire purpose of this
feature.

### SSH-Based Lock Files on GPU Hosts

Rejected. Creating lock files on each GPU host via SSH would work without a
shared filesystem, but it adds N SSH round-trips per reservation check and
complicates cleanup when an agent crashes. The shared filesystem is already a
prerequisite for the MCP runner model (ADR 0003); using it for coordination is
consistent.

### Agent-Side Coordination Only

Rejected. Telling agents to "just check `nvidia-smi` and tolerate collisions"
does not solve the stated problem. At small scale, collisions are rare but
wasteful: two jobs land on the same GPU, memory contention causes OOM or severe
slowdown, and the agent must retry. The reservation registry eliminates this
class of failure cheaply.

### Full Scheduler with Queuing and Priorities

Rejected. Out of scope. ADR 0001 explicitly excludes general HPC scheduler
features. The registry is a reservation list, not a job queue.

## Testing Implications

Deterministic tests should cover:

- registry is created on first reservation if absent;
- `check_gpus` marks reserved GPUs as `RESERVED` with age;
- `run_python_on_gpu` refuses a GPU reserved by another repo;
- `run_python_on_gpu` succeeds and writes a reservation for an unreserved GPU;
- reservation includes correct `host`, `gpu_index`, `repo`, `expires_at`;
- async launch updates reservation with remote PID;
- sync job completion removes reservation;
- `release_gpu_reservation` removes the matching entry;
- expired reservations are cleaned on `check_gpus` read;
- registry write uses atomic `os.replace` (no partial writes visible);
- concurrent reservation attempts from two MCP processes: one succeeds, one fails
  or retries;
- launch failure removes reservation (no orphaned GPU blocks).

Battlefield tests should cover:

1. Agent A in repo-a calls `check_gpus`, sees GPU 0 as `AVAILABLE`.
2. Agent A calls `run_python_on_gpu` on GPU 0; reservation is written.
3. Agent B in repo-b calls `check_gpus`, sees GPU 0 as `RESERVED`.
4. Agent B picks GPU 1 instead.
5. Agent A's job finishes; reservation is removed.
6. Agent B (or a new Agent C) later sees GPU 0 as `AVAILABLE` again.

## Consequences

This is a localized addition to the MCP tool surface, not a redesign. The
existing policy model, approval lifecycle, staged runner, and SSH fabric remain
unchanged.

The registry adds a small trusted state file under `~/.gpu-mcp/state/`, alongside
the approved-policy record. This directory is already treated as trusted control
state; adding one JSON file does not change the trust model.

The design stays lightweight: no daemon, no database, no new dependencies. The
implementation is roughly 50–100 lines of JSON file I/O with atomic replacement,
plus reservation checks in `check_gpus` and `run_python_on_gpu`.

The trade-off is explicit: the filesystem registry is not a distributed lock. On
a poorly coherent shared filesystem, the read-modify-write race window could
allow occasional collisions. For 1–10 agents in a single lab, this is acceptable.
If the lab grows beyond that scale, the correct migration is to a real scheduler
(e.g., Slurm, Kubernetes), not a more complex MCP-native coordinator.
