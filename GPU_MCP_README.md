# GPU MCP Setup

This is the setup guide for `gpu-cluster-mcp`, a Codex stdio MCP server that
runs approved Python files on GPU hosts mounted on the same shared filesystem.

The AI-native installer entry point is
`AI_native_installer/INSTALL_FOR_AI.md`. This README is the human-facing
overview and should not be treated as a replacement for the installer
checklist.

For the shortest practical path to enable the already-installed MCP in another
repo, start with `INSTALL.md`.

The install must prove two things:

1. Repo-local Codex config can load the MCP with this repo's `gpu-mcp.toml`.
2. At least one non-local GPU host works through MCP.

Do not mark the install complete after only a local smoke probe.

## 0. Values

There are two locations:

```text
GPU_MCP_INSTALL=/home/USER/gpu-mcp
RESEARCH_REPO=/shared/path/to/research-repo
PY=/absolute/path/to/python
SERVER=/home/USER/gpu-mcp/gpu_mcp_server.py
BOOTSTRAP=/home/USER/gpu-mcp/gpu_mcp_bootstrap.py
KEY=~/.ssh/gpu_mcp_key
INVENTORY=~/.cache/gpu-mcp/bootstrap_hosts.json
```

The MCP code is installed once. Each research repo gets its own
`gpu-mcp.toml` and `.codex/config.toml`.

The `PY` executable must have the runtime dependencies for the MCP server and
the GPU libraries needed by jobs. Hostnames are not hard-coded in Python; they
come from the human bootstrap inventory and the repo-local `gpu-mcp.toml`.

## 1. Python Preflight

Run on the control host, meaning the machine where Codex itself is running:

```bash
cd "$GPU_MCP_INSTALL"
"$PY" -c "import mcp.server.fastmcp; import paramiko; import fabric; print('GPU MCP Python deps OK')"
"$PY" -m py_compile gpu_mcp_server.py gpu_mcp_bootstrap.py
```

Required output:

```text
GPU MCP Python deps OK
```

If this fails, do not edit Codex config yet. Fix the Python environment first. At minimum, the server Python needs:

```bash
"$PY" -m pip install mcp paramiko fabric
```

Do this in the intended environment for `PY`, not in a random shell Python. The probe also needs either CUDA JAX or CUDA Torch on the host where the probe runs.

## 2. SSH Bootstrap

Remote MCP calls use this route:

```text
Codex on CONTROL_HOST -> gpu_mcp_server.py -> SSH to REMOTE_HOST -> guarded Python job
```

The MCP server uses a dedicated key at `~/.ssh/gpu_mcp_key` by default. First check whether that route already works:

```bash
"$PY" "$BOOTSTRAP" --verify-only --hosts-file "$GPU_MCP_INSTALL/hosts.txt" --inventory "$INVENTORY"
```

If the key is missing or targets fail verification, the human should create
`$GPU_MCP_INSTALL/hosts.txt` with one intended GPU host per line, then run the
interactive bootstrap in a real terminal on the control host:

```bash
"$PY" "$BOOTSTRAP" --install --hosts-file "$GPU_MCP_INSTALL/hosts.txt" --inventory "$INVENTORY"
```

The bootstrap asks once for the user's normal SSH password, appends the
dedicated public key to each destination `~/.ssh/authorized_keys`, then
verifies no-prompt login with the exact key MCP will use. The password is not
written to the repo or Codex config.

Root is not required if the user can log in normally and write their own destination-side `~/.ssh/authorized_keys`. Some historical nodes may be offline or not provisioned for this account; the MCP install requires at least one non-local GPU target that verifies with the dedicated key.

## 3. Codex Config

### Repo-Local MCP Server Config

Do not put repo-specific MCP policy paths in global `~/.codex/config.toml`.
Each research repo should contain its own Codex config:

```text
$RESEARCH_REPO/.codex/config.toml
```

Use the same MCP name in every repo:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/absolute/path/to/python"
args = [
  "/home/USER/gpu-mcp/gpu_mcp_server.py",
  "--config",
  "/shared/path/to/research-repo/gpu-mcp.toml"
]
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 360

[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.check_gpu_processes]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
# Codex shows this prompt only in the human UI. After approval, the agent sees
# the normal MCP result and cannot tell from the result that approval happened.
approval_mode = "prompt"
```

The server does not rely on `cwd` and does not read `GPU_MCP_CONFIG`. The
`--config` argument is the policy source.

The legacy global `[mcp_servers.gpu-cluster]` entry may exist for older live
sessions, but new installs should use repo-local `gpu-cluster-mcp`.

### User-Global Companion Hook

Install the GPU MCP companion hook once in the user's global Codex config:

```text
~/.codex/config.toml
```

```toml
[[hooks.PreToolUse]]
matcher = "*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /home/USER/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /home/USER/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "/absolute/path/to/python /home/USER/gpu-mcp/gpu_mcp_policy_hook.py"
timeout = 31536000
statusMessage = "Waiting for a managed GPU job event"
```

Restart Codex after editing Codex config. Use `/hooks` in Codex to review and
trust the user-global GPU MCP companion hook. The hook discovers the current
repo from the nearest `gpu-mcp.toml` and exits quietly outside GPU MCP repos. It
is workflow feedback: it warns the agent if `gpu-mcp.toml` has changed but has
not been reloaded. During active work, PreToolUse immediately surfaces a local
job outcome or a due status check. At turn end, Stop waits for either event and
continues the turn so the agent can call `status`. The one-year Stop timeout is
an operational hook-runner watchdog, not a managed-job cadence cap; increase it
if a deployment intentionally suspends turns longer than a year. Hooks never
parse outcomes or release reservations; the server remains the lifecycle and
safety boundary.

Running jobs may expose useful intermediate artifacts. Files the application
has already closed or atomically published may be inspected read-only and
reported as provisional. Provisional evidence may drive live scientific
decisions, including stopping. Terminal `status` is required only to claim that
the job completed or that an artifact is its final result. The server still
checks that the agent owns the job and is signaling the right process; it does
not decide whether the science is worth continuing. GPU MCP does not determine
whether an application-specific file is durable.

### Timing model

- Reservation heartbeats are lease safety. New jobs use a 10-minute lease
  interval, the local manager scans once per second and writes at least once per
  minute, and staleness is three lease intervals after the last successful
  write. The 60-minute heartbeat validation maximum is not a status-poll cap.
- Agent polling is repo-local guidance. A positive `cadence_hint_sec` is used
  exactly with no policy maximum; `expected_duration_sec` and smoke runtime are
  descriptive only. Without a hint, smoke jobs use five minutes and main or
  one-off jobs use one hour.
- Due/outcome re-reminders are throttled using that job's repo-local
  `poll_interval_sec`, never the shared heartbeat interval.
- Stop checks local timestamps and outcome-file presence once per second so it
  can notice early completion. That local scan is not an MCP status poll and
  does not change the job's polling schedule.

## 4. Repo Policy

Create `$RESEARCH_REPO/gpu-mcp.toml` from
`contracts/gpu-mcp.template.toml`. The installer AI should propose values from
the bootstrap inventory, but the human approves safety-relevant policy:

```toml
schema_version = 1
repo_root = "/shared/path/to/research-repo"
nodes = ["gpu01.example.edu"]
script_roots = ["jobs"]
write_roots = ["results"]
output_roots = [".gpu_mcp_logs"]
sync_timeout_sec = 300
```

`nodes` is a repo-specific allowlist. The bootstrap inventory proves a host is
reachable; it does not grant repo permission by itself.

Remote GPU jobs use a standalone safe runner staged into the research repo under
`.gpu_mcp_runner/`. Remote hosts do not need the full MCP server installation
path, but they must be able to see the research repo, the job script, and the
staged runner through the shared filesystem. Keep `.gpu_mcp_runner/` out of
version control.

Approve the policy before starting Codex with this MCP server:

```bash
python /home/USER/gpu-mcp/gpu_mcp_doctor.py approve-policy \
  --config /absolute/path/to/research-repo/gpu-mcp.toml \
  --yes
```

The approval record is stored outside the repo under
`~/gpu-mcp/state/approved-policies.json`. If `gpu-mcp.toml` changes later, the
MCP server refuses normal cluster tools until the changed policy is previewed
and explicitly reloaded, and it refuses to start in a new session until the
changed policy is approved.

Validate readiness with `gpu_mcp_doctor.py check --config
/absolute/path/to/research-repo/gpu-mcp.toml`, then run the real battlefield
suite when doing full acceptance.

Intentional same-session policy edits must use the explicit reload flow:

1. edit `gpu-mcp.toml` because the human asked for a policy change;
2. call `preview_policy_reload`;
3. show the raw safety-relevant diff and hash output to the human;
4. call `reload_policy` with the returned token only after explicit approval,
   or call `reject_policy_reload` if the human rejects the candidate.

If the human rejects or cancels a candidate and then asks for a different
candidate, make only the requested `gpu-mcp.toml` edit and return to
`preview_policy_reload`. Do not activate the rejected candidate just to make
another edit.

Do not edit policy as a workaround inside a blocked GPU task.

## 5. Local MCP Probe

Use the MCP tool, not a direct shell invocation of Python:

```text
run_python_on_gpu(
  host="gpu01.example.edu",
  gpu_index=0,
  script_path="/shared/path/to/research-repo/jobs/probe.py",
  args=["--sleep", "0"],
  async_mode=false
)
```

Required output fields:

```text
cuda_visible_devices=0
used_gpu=True
```

The probe tries JAX first and Torch second. A successful GPU probe should contain either `jax_devices=...cuda...` or `torch_cuda_available=True`.

## 6. Cluster MCP Status

Use the MCP tool:

```text
check_gpus(samples=1, threshold=10)
```

Required: the output starts with:

```text
==> Starting GPU check: 1 samples, threshold <= 10%
```

and includes at least one non-local RTX 4090 host block such as:

```text
[blob ssh]
  GPU 0 | NVIDIA GeForce RTX 4090 | ...
```

Host blocks are labeled `local` when checked on the control host itself and `ssh` when checked through the remote route. Some hosts may report `ssh/nvidia-smi failed`; that is acceptable if at least one non-local `ssh` target works and the remote probe in section 7 succeeds.

## 7. Remote MCP Probe

After SSH works, run the probe through the MCP tool to a non-local host. Do
not substitute a direct `ssh python ...` command for this test. The `host`
value must be allowed by this repo's `gpu-mcp.toml`.

```text
run_python_on_gpu(
  host="REMOTE_HOST",
  gpu_index=0,
  script_path="/shared/path/to/research-repo/jobs/probe.py",
  args=["--sleep", "0"],
  async_mode=false
)
```

Required output fields:

```text
host=REMOTE_HOSTNAME
cuda_visible_devices=0
used_gpu=True
```

As in the local probe, GPU proof can come from either JAX CUDA output or Torch CUDA output.

If local probe works but remote probe fails, the MCP is mounted but the remote route is not installed. Check:

```text
1. REMOTE_HOST is listed in this repo's `gpu-mcp.toml`.
2. SSH from the control host to that remote host works without prompts.
3. The remote host mounts the same repo path.
4. The remote host has a working GPU Python/JAX/Torch environment.
5. ~/.codex/log/codex-tui.log contains no MCP startup error.
```

## 8. Owner-Scoped Process Cancellation

Use `kill_gpu_process` only for a specific `host` and `pid`. The tool is
stateless and does not keep a registry. First call it without a fingerprint to
inspect exactly one process:

```text
kill_gpu_process(host="gpu01.example.edu", pid=1234)
```

The response includes the owner, process start time, process group, command
hash, short command preview, GPU index/memory when available, and a
`fingerprint`. The tool marks the process killable only when the owner exactly
matches `GPU_MCP_USER`.

To signal the process, call the tool again with the returned fingerprint:

```text
kill_gpu_process(
  host="gpu01.example.edu",
  pid=1234,
  fingerprint="gpu-mcp-kill-v1:...",
  signal="KILL"
)
```

Before sending the signal, the server re-reads the process and refuses if the
owner, start time, process group, or command hash changed. Supported signals are
`KILL` and `TERM`; `KILL` is the default. The tool does not inspect whether the
command is Python, inference code, or MCP-launched.

## 9. Job Logs

Async job logs must end in `.log` and be under one of:

```text
/shared/path/to/research-repo/.gpu_mcp_logs
/tmp/gpu_mcp_logs_for_this_repo
```

Example:

```text
output_file=".gpu_mcp_logs/job.log"
```

This path is only for stdout/stderr capture. It is not the general result directory for a script.

## 10. Script Result Writes

Scripts launched through `run_python_on_gpu` run under a safety guard. The guard blocks destructive APIs, subprocess launch, socket connections, and writes outside approved write roots. Local and remote jobs both run from the shared repo root, so relative result paths are relative to the repo.

This is not a hostile-code sandbox. It is a guardrail for trusted lab scripts
and common AI-generated mistakes. The guard controls execution, subprocess,
network, and write behavior; it does not prevent a script from reading files
that the Unix user can already read.

By default, script writes are allowed under:

```text
the configured write_roots in gpu-mcp.toml
```

For durable job results, prefer a path inside the repo, for example:

```text
/shared/path/to/research-repo/simulation_results/my_gpu_run
```

If a scratch/log directory outside the repo is needed, add an explicit
`/tmp/...` root to `write_roots` or `output_roots` in `gpu-mcp.toml`. Other
outside-repo roots are not part of v1.

Async output directories must already exist and must not be symlinks. The MCP
will not create output directories at job launch time.

```toml
write_roots = ["results", "/tmp/gpu_mcp_outputs_for_this_repo"]
```

The same absolute paths must be valid on the control host and remote GPU hosts.

If a job fails with:

```text
GPU MCP blocked write outside approved roots
```

move the script output under an approved write root or update `gpu-mcp.toml`
with human approval.

## 11. Completion Criteria

The installation is complete only when these are true:

```text
1. repo-local .codex/config.toml registers gpu-cluster-mcp with --config.
2. gpu-mcp.toml contains the human-approved repo policy.
3. doctor check passes for the repo-local policy/config.
4. raw remote command and Codex self-spawn prompt rules are verified without
   --ignore-rules.
5. a non-local run_python_on_gpu probe succeeds through MCP.
6. the user-global GPU MCP companion hook is installed and trusted in Codex.
7. the full real battlefield suite passes, or any omitted family is explicitly
   documented as unsafe to run in the current environment.
```
