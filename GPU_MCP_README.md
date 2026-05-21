# GPU MCP Setup

This is the complete install test for `gpu-cluster`, a Codex stdio MCP server that runs approved Python files on GPUs mounted on the same shared filesystem.

It is written for a fresh Unix account where nothing about Codex MCP, SSH keys, or the Python environment should be assumed to already work.

The agent installing this must prove two things:

1. `localhost` probe works through MCP.
2. one non-local GPU host probe works through MCP.

Do not mark the install complete after only `localhost`.

## 0. Values

For this repo:

```text
REPO=/net/levsha/scratch2/tingran/test_repo
PY=/home/tingran/miniconda3/bin/python
SERVER=/net/levsha/scratch2/tingran/test_repo/gpu_mcp_server.py
PROBE=/net/levsha/scratch2/tingran/test_repo/gpu_mcp_probe.py
KEY=~/.ssh/gpu_mcp_key
BOOTSTRAP=/net/levsha/scratch2/tingran/test_repo/gpu_mcp_bootstrap_ssh.py
```

For another repo or user, replace these values everywhere. The `PY` executable must have `mcp`, `fabric`, `paramiko`, and the GPU library used by jobs available. The remote host names must also be listed in `NODES` inside `gpu_mcp_server.py`.

## 1. Python Preflight

Run on the control host, meaning the machine where Codex itself is running:

```bash
cd /net/levsha/scratch2/tingran/test_repo
/home/tingran/miniconda3/bin/python -c "import mcp.server.fastmcp; import fabric; import paramiko; import gpu_mcp_server; print('gpu_mcp_server imports OK')"
/home/tingran/miniconda3/bin/python -m py_compile gpu_mcp_server.py gpu_mcp_probe.py gpu_mcp_bootstrap_ssh.py
```

Required output:

```text
gpu_mcp_server imports OK
```

If this fails, do not edit Codex config yet. Fix the Python environment first. At minimum, the server Python needs:

```bash
/home/tingran/miniconda3/bin/python -m pip install mcp fabric paramiko
```

Do this in the intended environment for `PY`, not in a random shell Python. The probe also needs either CUDA JAX or CUDA Torch on the host where the probe runs.

## 2. SSH Bootstrap

Remote MCP calls use this route:

```text
Codex on CONTROL_HOST -> gpu_mcp_server.py -> SSH to REMOTE_HOST -> guarded Python job
```

The MCP server uses a dedicated key at `~/.ssh/gpu_mcp_key` by default. First check whether that route already works:

```bash
/home/tingran/miniconda3/bin/python /net/levsha/scratch2/tingran/test_repo/gpu_mcp_bootstrap_ssh.py --verify-only blob.mit.edu
```

If the key is missing or the target fails verification, ask the user to run the interactive bootstrap in a real terminal on the control host:

```bash
/home/tingran/miniconda3/bin/python /net/levsha/scratch2/tingran/test_repo/gpu_mcp_bootstrap_ssh.py
```

With no host arguments, the bootstrap uses every host in `NODES` inside `gpu_mcp_server.py`. To inspect that list:

```bash
/home/tingran/miniconda3/bin/python /net/levsha/scratch2/tingran/test_repo/gpu_mcp_bootstrap_ssh.py --list-hosts
```

The bootstrap follows the saunasub model: it asks once for the user's normal SSH password, appends the dedicated public key to each destination `~/.ssh/authorized_keys`, then verifies no-prompt login with the exact key MCP will use. The password is not written to the repo or Codex config.

Root is not required if the user can log in normally and write their own destination-side `~/.ssh/authorized_keys`. Some historical nodes may be offline or not provisioned for this account; the MCP install requires at least one non-local GPU target that verifies with the dedicated key.

## 3. Codex Config

Edit the user-level Codex config on the control host:

```text
~/.codex/config.toml
```

There must be exactly one `[mcp_servers.gpu-cluster]` table. Replace any old `gpu-cluster` table with:

```toml
[mcp_servers.gpu-cluster]
command = "/home/tingran/miniconda3/bin/python"
args = ["/net/levsha/scratch2/tingran/test_repo/gpu_mcp_server.py"]
cwd = "/net/levsha/scratch2/tingran/test_repo"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
  "check_gpus",
  "check_gpu_processes",
  "cluster_info",
  "kill_gpu_process",
  "run_python_on_gpu",
]
```

By default, the MCP SSH username is the Unix user running Codex, and remote jobs run with the same Python executable used to launch the MCP server. If either default is wrong, add one env table and include only the keys needed:

```toml
[mcp_servers.gpu-cluster.env]
GPU_MCP_USER = "USER"
GPU_MCP_PYTHON = "/ABS/PATH/TO/python"
GPU_MCP_SSH_KEY = "/ABS/PATH/TO/.ssh/gpu_mcp_key"
```

Use only one `[mcp_servers.gpu-cluster.env]` table. If later steps add `GPU_MCP_WRITE_ROOTS`, put it in the same table.

Codex CLI uses `~/.codex/config.toml`. Do not rely on repo-local `.codex/config.toml`, and do not rely on `.mcp.json`.

Restart Codex after editing this file.

## 4. Tool Registration

After restart:

```bash
codex mcp get gpu-cluster
```

Required fields:

```text
enabled: true
enabled_tools: check_gpus, check_gpu_processes, cluster_info, kill_gpu_process, run_python_on_gpu
command: /home/tingran/miniconda3/bin/python
args: /net/levsha/scratch2/tingran/test_repo/gpu_mcp_server.py
cwd: /net/levsha/scratch2/tingran/test_repo
```

Inside Codex, `/mcp` must show the same five tools. `Auth: Unsupported` is normal. `Tools: (none)` is a failure.

If tools are missing, inspect `~/.codex/log/codex-tui.log`. Usual causes are: Codex was not restarted, wrong Python path, missing Python packages, duplicate `gpu-cluster` config, or project config not trusted.

## 5. Local MCP Probe

Use the MCP tool, not a direct shell invocation of Python:

```text
run_python_on_gpu(
  host="localhost",
  gpu_index=0,
  script_path="/net/levsha/scratch2/tingran/test_repo/gpu_mcp_probe.py",
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

After SSH works, run the probe through the MCP tool to a non-local host. Do not substitute a direct `ssh python ...` command for this test. The `host` value must be accepted by `NODES` in `gpu_mcp_server.py`.

```text
run_python_on_gpu(
  host="REMOTE_HOST",
  gpu_index=0,
  script_path="/net/levsha/scratch2/tingran/test_repo/gpu_mcp_probe.py",
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
1. REMOTE_HOST is listed in NODES.
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
kill_gpu_process(host="blob.mit.edu", pid=1234)
```

The response includes the owner, process start time, process group, command
hash, short command preview, GPU index/memory when available, and a
`fingerprint`. The tool marks the process killable only when the owner exactly
matches `GPU_MCP_USER`.

To signal the process, call the tool again with the returned fingerprint:

```text
kill_gpu_process(
  host="blob.mit.edu",
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
/net/levsha/scratch2/tingran/test_repo/.gpu_mcp_logs
/tmp/gpu_mcp_logs
```

Example:

```text
output_file="/net/levsha/scratch2/tingran/test_repo/.gpu_mcp_logs/job.log"
```

This path is only for stdout/stderr capture. It is not the general result directory for a script.

## 10. Script Result Writes

Scripts launched through `run_python_on_gpu` run under a safety guard. The guard blocks destructive APIs, subprocess launch, socket connections, and writes outside approved write roots. Local and remote jobs both run from the shared repo root, so relative result paths are relative to the repo.

By default, script writes are allowed under:

```text
/net/levsha/scratch2/tingran/test_repo
/tmp/gpu_mcp_logs
/tmp/gpu_mcp_outputs
/tmp/gpu_mcp_matplotlib_cache
```

For durable job results, prefer a path inside the repo, for example:

```text
/net/levsha/scratch2/tingran/test_repo/simulation_results/my_gpu_run
```

If a result directory outside the repo is needed, add `GPU_MCP_WRITE_ROOTS` to the same env table in `~/.codex/config.toml`. Use colon-separated paths:

```toml
[mcp_servers.gpu-cluster.env]
GPU_MCP_USER = "USER"
GPU_MCP_WRITE_ROOTS = "/ABS/PATH/TO/results:/ABS/PATH/TO/other_results"
```

Then restart Codex. The same absolute paths must be valid on the control host and remote GPU hosts.

If a job fails with:

```text
GPU MCP blocked write outside approved roots
```

move the script output under an approved write root or add the intended result directory to `GPU_MCP_WRITE_ROOTS`.

## 11. Completion Criteria

The installation is complete only when all five are true:

```text
1. codex mcp get gpu-cluster lists the five expected tools.
2. /mcp inside Codex lists the five expected tools.
3. check_gpus(samples=1, threshold=10) returns cluster GPU status through MCP.
4. run_python_on_gpu(... host="localhost" ..., gpu_mcp_probe.py ...) returns used_gpu=True.
5. run_python_on_gpu(... host="REMOTE_HOST" ..., gpu_mcp_probe.py ...) returns used_gpu=True.
```
