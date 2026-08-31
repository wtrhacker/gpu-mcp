# GPU MCP

Run policy-constrained Python jobs on lab GPU hosts through the Model Context
Protocol (MCP).

GPU MCP gives an AI coding agent a controlled path to inspect NVIDIA GPUs,
reserve a device, launch a Python file, monitor the resulting job, and stop a
specific owned process. The human defines the permitted hosts and filesystem
roots in a repo-local policy; the server refuses unapproved or stale policy.

> [!IMPORTANT]
> GPU MCP is an experimental guardrail for trusted lab environments. It is not
> a hostile-code sandbox, a scheduler backend, or an isolation boundary between
> mutually untrusted users.

## Who it is for

GPU MCP currently fits environments where:

- a Linux control host can SSH directly to one or more Linux NVIDIA GPU hosts;
- the control host and GPU hosts see the research repository at the same
  absolute path through a shared filesystem;
- the same Unix user is authorized to run work on those hosts; and
- Python jobs, rather than arbitrary shell commands, are the execution unit.

It is not yet designed for SLURM/PBS/LSF-only clusters, Kubernetes, cloud GPU
rental APIs, or hostile multi-tenant execution.

## What it provides

- Cluster and process inspection through `nvidia-smi`.
- Repo-local allowlists for hosts, Python scripts, result directories, log
  directories, GPU names, and minimum free memory.
- Human-reviewed policy approval plus preview/reload handling for later policy
  changes.
- Managed job handles, GPU reservations, heartbeat-based liveness, status
  cadence, retry, finish, and owner-scoped stop operations.
- Static and runtime guards against common destructive APIs, subprocesses,
  sockets, and writes outside approved roots.
- Fingerprint-confirmed signaling of one process owned by the configured user.
- Contract tests, real Codex integration tests, and an opt-in live GPU
  battlefield suite.

## Architecture

```text
Codex
  │  stdio MCP
  ▼
gpu_mcp_server.py on the control host
  │
  ├── local nvidia-smi / guarded Python
  │
  └── dedicated SSH key
        ▼
      approved GPU host
        ▼
      staged safe runner + Python job

Shared research repository
  ├── gpu-mcp.toml       human-approved policy
  ├── .codex/config.toml project-scoped MCP registration
  ├── jobs/              approved Python files
  ├── results/           approved durable writes
  └── .gpu_mcp_logs/     approved stdout/stderr logs
```

The MCP installation and each research repository are separate. Install GPU MCP
once on the control host; give every research repo its own `gpu-mcp.toml` and
`.codex/config.toml`.

## Requirements

- Linux and Python 3.11 or newer on the control host.
- `mcp`, `fabric`, and `paramiko` in the Python environment that starts the
  server.
- A Python executable available at the same absolute path on the control host
  and GPU hosts. This is the interpreter used for staged jobs; set it with
  `GPU_MCP_PYTHON` when it differs from the server interpreter.
- OpenSSH on the control host and remote GPU hosts.
- `nvidia-smi` on each GPU host.
- A shared filesystem path visible at the same absolute location on every host.
- A local Codex client for the configuration shown below. Codex supports local
  stdio MCP servers in a trusted project's `.codex/config.toml`; see the
  [official MCP documentation](https://developers.openai.com/codex/mcp/).

The job environment also needs whichever CUDA-enabled library the research
script uses, such as JAX or PyTorch.

## Quickstart

Use absolute paths throughout setup. The examples below distinguish the GPU MCP
installation from the research repository:

```bash
GPU_MCP_HOME=/absolute/path/to/gpu-mcp
RESEARCH_REPO=/shared/path/to/research-repo
GPU_MCP_SERVER_PYTHON="$GPU_MCP_HOME/.venv/bin/python"
GPU_JOB_PYTHON=/shared/path/to/gpu-capable/python
```

`GPU_JOB_PYTHON` may be the same interpreter as `GPU_MCP_SERVER_PYTHON` when
that virtual environment is visible on every host. Otherwise, use a shared
Python environment containing the CUDA libraries required by the jobs.

### 1. Clone and install dependencies

```bash
git clone https://github.com/wtrhacker/gpu-mcp.git "$GPU_MCP_HOME"
cd "$GPU_MCP_HOME"
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install mcp fabric paramiko
```

For development and contract tests, also install:

```bash
python -m pip install pytest jsonschema
```

This repository is currently script-based; `pyproject.toml` records dependency
metadata but is not yet configured as an installable Python distribution.

### 2. Bootstrap dedicated SSH access

Run the bootstrap interactively on the control host with the exact GPU hosts you
intend to authorize:

```bash
cd "$GPU_MCP_HOME"
"$GPU_MCP_SERVER_PYTHON" gpu_mcp_bootstrap.py --install \
  gpu01.example.edu gpu02.example.edu
```

The bootstrap creates `~/.ssh/gpu_mcp_key` if needed, asks the human for the
normal SSH password, installs only the public key on the selected hosts, verifies
non-interactive login, and writes an inventory to
`~/.cache/gpu-mcp/bootstrap_hosts.json`. To check an existing setup without
installing a key, omit `--install`.

Only put verified, human-approved hosts into a research repo policy.

### 3. Create the repo-local policy

Create the working directories in the research repo:

```bash
cd "$RESEARCH_REPO"
mkdir -p jobs results .gpu_mcp_logs
```

Then create `$RESEARCH_REPO/gpu-mcp.toml`:

```toml
schema_version = 1
repo_root = "/shared/path/to/research-repo"

nodes = ["gpu01.example.edu", "gpu02.example.edu"]

script_roots = ["jobs"]
write_roots = ["results"]
output_roots = [".gpu_mcp_logs"]

allowed_gpu_names = []
min_free_memory_mib = 0
sync_timeout_sec = 300
```

`repo_root` must exactly match the directory containing `gpu-mcp.toml`. Relative
roots are resolved under that directory. `script_roots` must stay inside the
repo; write and output roots may be inside the repo or under `/tmp`. See
[`contracts/gpu-mcp.template.toml`](contracts/gpu-mcp.template.toml) for the
template used by the installer.

### 4. Approve the policy

Review the complete policy, then record explicit approval:

```bash
"$GPU_MCP_SERVER_PYTHON" "$GPU_MCP_HOME/gpu_mcp_doctor.py" approve-policy \
  --config "$RESEARCH_REPO/gpu-mcp.toml" \
  --yes
```

Approval is bound to the policy hash. If the file changes, normal cluster tools
refuse to run until the candidate is previewed and explicitly reloaded.

### 5. Register the MCP server with Codex

Create `$RESEARCH_REPO/.codex/config.toml`, replacing every example path:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/absolute/path/to/gpu-mcp/.venv/bin/python"
args = [
  "/absolute/path/to/gpu-mcp/gpu_mcp_server.py",
  "--config",
  "/shared/path/to/research-repo/gpu-mcp.toml",
]
cwd = "/shared/path/to/research-repo"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 360

[mcp_servers.gpu-cluster-mcp.env]
# Must resolve on the control host and every configured GPU host.
GPU_MCP_PYTHON = "/shared/path/to/gpu-capable/python"

[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.check_gpus]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.check_gpu_processes]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.cluster_info]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.manage_gpu_job]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.list_gpu_reservations]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.preview_policy_reload]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.reject_policy_reload]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
approval_mode = "prompt"
```

Keep `tool_timeout_sec` greater than the policy's `sync_timeout_sec`. The current
doctor expects automatic approval for operational tools and a human prompt for
`reload_policy`. Codex documents project-scoped MCP configuration and per-tool
approval modes in its
[MCP guide](https://developers.openai.com/codex/mcp/) and
[configuration reference](https://developers.openai.com/codex/config-reference/).

### 6. Run the doctor and start Codex

```bash
"$GPU_MCP_SERVER_PYTHON" "$GPU_MCP_HOME/gpu_mcp_doctor.py" check \
  --config "$RESEARCH_REPO/gpu-mcp.toml" \
  --json

cd "$RESEARCH_REPO"
codex
```

Trust the project when Codex asks; untrusted projects do not load project-local
`.codex` configuration. Start with read-only inspection:

```text
cluster_info()
check_gpus(samples=1, threshold=10)
check_gpu_processes()
```

Then launch an approved file as a managed job:

```text
run_python_on_gpu(
  host="gpu01.example.edu",
  gpu_index=0,
  script_path="/shared/path/to/research-repo/jobs/probe.py",
  args=[],
  job_role="smoke"
)
```

The call returns a job handle. Use its `job_id` with
`manage_gpu_job(action="status", job_id="...")` and follow the returned
`next_poll_after` guidance.

## Companion hook

`gpu_mcp_policy_hook.py` is an optional but recommended Codex lifecycle
companion. It reports stale policy, due status checks, and managed-job outcomes;
the MCP server remains the lifecycle and safety authority.

Install the hook once in the user's `~/.codex/config.toml`, using absolute paths:

```toml
[[hooks.PreToolUse]]
matcher = "*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 31536000
statusMessage = "Waiting for a managed GPU job event"
```

Restart Codex and use `/hooks` to inspect and trust the exact hook definition.
Codex skips new or changed non-managed hooks until they are trusted. See the
[official hooks documentation](https://developers.openai.com/codex/hooks/).

## Tool reference

| Tool | Purpose |
| --- | --- |
| `cluster_info` | Summarize node reachability, GPU count, utilization, and load. |
| `check_gpus` | Sample device utilization and overlay reservation state. |
| `check_gpu_processes` | Show GPU PIDs, owners, commands, and memory use. |
| `run_python_on_gpu` | Reserve a device and launch an approved Python file as a managed job. |
| `manage_gpu_job` | Inspect status or update, stop, retry, or finish an owned job. |
| `list_gpu_reservations` | Recover or diagnose active reservations. |
| `kill_gpu_process` | Inspect, fingerprint, and signal one owned process. |
| `preview_policy_reload` | Validate a changed policy and return its safety-relevant diff and one-time token. |
| `reload_policy` | Activate a previously previewed, human-approved candidate. |
| `reject_policy_reload` | Discard a pending reload token without changing active policy. |

### Managed launch flow

For staged work, use `job_role="smoke"` first. A later `job_role="main"`
launch must cite the successful smoke job or include an explicit reason for
skipping it. `cadence_hint_sec` sets the requested status interval;
`expected_duration_sec` is descriptive metadata. `run_python_on_gpu` returns a
managed handle regardless of the legacy `async_mode` value.

Reservations are per user, host, and GPU. A local heartbeat manager renews the
lease while the process identity remains valid. Status calls verify process and
outcome state; `finish` releases a terminal reservation. A live job's closed or
atomically published artifacts may be useful as provisional evidence, but only
terminal status establishes final completion.

### Process signaling

`kill_gpu_process` is deliberately two-step:

1. Call it with a specific `host` and `pid`, but no fingerprint.
2. Review the owner, GPU, start time, process group, and command preview.
3. Call it again with the returned fingerprint and `signal="TERM"` or
   `signal="KILL"`.

The second call re-reads the process and refuses if its identity changed or its
owner does not match the configured GPU MCP user.

## Safety model

GPU MCP reduces common agent mistakes; it does not make untrusted Python safe.

- The server allowlists hosts and Python files before launch.
- The runtime guard blocks selected destructive APIs, subprocess creation,
  sockets, and writes outside approved roots.
- The Unix account's existing read permissions still apply. A launched script
  can read files that account can read.
- Remote jobs run with the SSH user's normal OS permissions.
- Client-side rules must prevent an agent from bypassing MCP with raw `ssh`,
  `scp`, `rsync`, or a second unrestricted agent process.
- Policy changes require a hash-bound preview/reload flow, but the human still
  needs to inspect the proposed diff.
- Async log directories must already exist and must not be symlinks.

Use narrow script, write, and output roots. Prefer `TERM` before `KILL`, keep
interactive approvals enabled during setup, and do not deploy this as a security
boundary for mutually untrusted users.

## Testing

Run the local contract suite:

```bash
cd "$GPU_MCP_HOME"
pytest -q -m contract
```

Compile the main entry points:

```bash
python -m py_compile \
  gpu_mcp_server.py \
  gpu_mcp_bootstrap.py \
  gpu_mcp_doctor.py \
  gpu_mcp_policy_hook.py
```

Live Codex and GPU tests are opt-in because they start real clients, use SSH,
and may launch jobs. Maintainers can run the full real battlefield only in a
prepared environment:

```bash
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 \
  pytest -q test/test_real_gpu_mcp_battlefield.py
```

See [`test/README.md`](test/README.md) and
[`test/BATTLEFIELD.md`](test/BATTLEFIELD.md) for test boundaries and evidence
requirements.

## Repository guide

- [`AI_native_installer/INSTALL_FOR_AI.md`](AI_native_installer/INSTALL_FOR_AI.md):
  detailed checklist for an AI helping a human configure a new research repo.
- [`gpu_mcp_server.py`](gpu_mcp_server.py): stdio MCP server and managed job
  lifecycle.
- [`gpu_mcp_config.py`](gpu_mcp_config.py): repo policy parser and root
  validation.
- [`gpu_mcp_bootstrap.py`](gpu_mcp_bootstrap.py): human-first dedicated SSH key
  bootstrap.
- [`gpu_mcp_doctor.py`](gpu_mcp_doctor.py): policy approval and setup checks.
- [`gpu_mcp_guard.py`](gpu_mcp_guard.py): static and runtime Python guardrails.
- [`gpu_mcp_reservations.py`](gpu_mcp_reservations.py): shared reservation
  registry.
- [`ADR/`](ADR/): design decisions and implementation evidence.
- [`contracts/`](contracts/): policy, inventory, result templates, and schemas.

`INSTALL.md` documents one existing machine-specific deployment and contains
site-specific example paths. New installations should start with this README or
the AI-native installer instead of copying those paths literally.

## Project status and license

The package version is currently `0.0.0`, and the project should be treated as
experimental. No open-source license has been selected yet; public availability
does not by itself grant permission to copy, modify, or redistribute the code.
