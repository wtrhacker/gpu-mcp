# Configure GPU MCP for a Research Repository

This guide is for an AI assistant configuring one research repository with a
human in the loop.

The control-side GPU MCP runtime must already be installed. This guide does not
choose, clone, synchronize, or relocate that runtime. Its first job is to
establish the exact deployment facts supplied by the human.

## Outcome

A completed research repository has:

```text
research-repo/
  .codex/config.toml   # project-scoped MCP registration
  gpu-mcp.toml         # human-approved repository policy
  jobs/                # or another approved script root
  results/             # or another approved write root
  .gpu_mcp_logs/       # or another approved output root
```

The GPU MCP implementation remains in one stable runtime directory on the
control host. Do not copy it into every research repository.

## Required deployment facts

Before editing the research repository, establish all of these values:

1. `GPU_MCP_ROOT`: the installed control-side runtime containing
   `gpu_mcp_server.py`, `gpu_mcp_policy_hook.py`, and `installation_test.py`.
2. `GPU_MCP_SERVER_PYTHON`: the control-host interpreter that launches the MCP
   server and can import MCP SDK v1, Fabric, and Paramiko.
3. `GPU_JOB_PYTHON`: the interpreter used for launched jobs. Its absolute path
   must resolve on the control host and every selected GPU host.
4. `RESEARCH_REPO`: the exact repository root the human wants to authorize.
5. The bootstrap inventory path, normally
   `~/.cache/gpu-mcp/bootstrap_hosts.json`.

Do not infer `GPU_MCP_ROOT` from the current working directory, another Git
checkout, a similarly named directory, or a path found in an unrelated repo.
The development checkout and installed runtime may be different directories.

A maintainer may provide a gitignored `LOCAL_DEPLOYMENT.md` containing
site-specific paths and synchronization instructions. Use it as local context
when present, but never copy its infrastructure details into tracked public
documentation.

If `GPU_MCP_ROOT`, `GPU_MCP_SERVER_PYTHON`, or `RESEARCH_REPO` is missing or
ambiguous, stop and ask the human for that fact. Do not repair ambiguity by
repointing Codex to whichever checkout happens to be available.

## Validate the runtime before configuring the repo

Substitute the confirmed absolute paths and perform equivalent read-only checks:

```bash
test -f /absolute/gpu-mcp-runtime/gpu_mcp_server.py
test -f /absolute/gpu-mcp-runtime/gpu_mcp_policy_hook.py
test -x /absolute/path/to/server-python

/absolute/path/to/server-python - <<'PY'
from importlib.metadata import version

import fabric
import mcp.server.fastmcp
import paramiko

mcp_version = version("mcp")
assert int(mcp_version.split(".", 1)[0]) == 1, mcp_version
print(f"GPU MCP runtime imports OK (mcp={mcp_version})")
PY
```

If the files are missing, the runtime needs deployment or repair using
`INSTALL.md` and any local deployment runbook. If imports fail, repair the
confirmed server interpreter. Do not substitute the research checkout as the
runtime.

## Human bootstrap boundary

Read the bootstrap inventory. It is evidence of which hosts accepted the
dedicated GPU MCP SSH key; it is not repository policy and does not authorize
every verified host.

Only hosts with `"status": "verified"` are candidates. If the inventory is
missing or contains no verified non-local host, ask the human to run:

```bash
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_bootstrap.py \
  --install gpu01.example.edu gpu02.example.edu
```

The human must choose the hostnames and complete interactive SSH authentication
or host-trust prompts. Do not create or install SSH credentials silently.

## Step 1: Confirm the research repository

Resolve the human-provided repository root and confirm that it is the directory
to authorize. The future `gpu-mcp.toml` must live directly in this directory,
and its `repo_root` must match the resolved path exactly.

Copy the installation journal into the research repository:

```bash
cp /absolute/gpu-mcp-runtime/AI_native_installer/progress.template.md \
  /absolute/research-repo/progress.md
```

Update it as setup proceeds. The journal is not proof; tool responses and
diagnostic output are the evidence.

## Step 2: Propose the policy boundary

Propose, but do not silently decide:

- a subset of the verified bootstrap hosts;
- the Python directories whose files the agent may launch;
- durable result directories launched code may modify;
- stdout/stderr output directories;
- optional allowed GPU names and minimum free memory; and
- a synchronous timeout appropriate for the workload.

Prefer narrow existing directories. For a new repository, a reasonable initial
proposal is:

| Policy field | Initial proposal |
| --- | --- |
| `script_roots` | `["jobs"]` |
| `write_roots` | `["results"]` |
| `output_roots` | `[".gpu_mcp_logs"]` |

Do not use the whole repository or a broad temporary directory merely for
convenience. Explain any additional root before writing it.

## Step 3: Write the repo-local Codex registration first

Create or merge `RESEARCH_REPO/.codex/config.toml`. Replace every placeholder
with the confirmed absolute value:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/ABSOLUTE/GPU_MCP_SERVER_PYTHON"
args = [
  "/ABSOLUTE/GPU_MCP_ROOT/gpu_mcp_server.py",
  "--config",
  "/ABSOLUTE/RESEARCH_REPO/gpu-mcp.toml",
]
cwd = "/ABSOLUTE/RESEARCH_REPO"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 360

[mcp_servers.gpu-cluster-mcp.env]
GPU_MCP_PYTHON = "/ABSOLUTE/GPU_JOB_PYTHON"

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

# Codex must prompt the human at the activation boundary.
[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
approval_mode = "prompt"
```

Keep this MCP registration repo-local. Do not put a research-repo policy path
in the user's global Codex configuration. `tool_timeout_sec` must be greater
than the policy's `sync_timeout_sec`.

Config comes first because the server can start without active policy authority.
A missing, invalid, or unapproved policy places it in `bootstrap_pending`
quarantine while leaving the three policy recovery tools available.

## Step 4: Install or verify the global companion controls

The policy hook is user-global because it discovers the nearest current-repo
`gpu-mcp.toml`. Add it once to `~/.codex/config.toml`, using the confirmed
runtime and server Python paths. Do not duplicate existing equivalent hooks.

```toml
[[hooks.PreToolUse]]
matcher = "*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/ABSOLUTE/GPU_MCP_SERVER_PYTHON /ABSOLUTE/GPU_MCP_ROOT/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "/ABSOLUTE/GPU_MCP_SERVER_PYTHON /ABSOLUTE/GPU_MCP_ROOT/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "/ABSOLUTE/GPU_MCP_SERVER_PYTHON /ABSOLUTE/GPU_MCP_ROOT/gpu_mcp_policy_hook.py"
timeout = 31536000
statusMessage = "Waiting for a managed GPU job event"
```

Also verify the user's Codex command policy prompt-gates raw `ssh`, `scp`,
`sftp`, `rsync`, and self-spawned `codex` commands. Show the human any proposed
global hook or rule change before relying on it. Never run validation with
`--ignore-rules`.

## Step 5: Write the policy candidate

Create the approved directories, then write `RESEARCH_REPO/gpu-mcp.toml` using
the values proposed to the human. For example:

```toml
schema_version = 1
repo_root = "/ABSOLUTE/RESEARCH_REPO"

nodes = ["gpu01.example.edu", "gpu02.example.edu"]

script_roots = ["jobs"]
write_roots = ["results"]
output_roots = [".gpu_mcp_logs"]

allowed_gpu_names = []
min_free_memory_mib = 0
sync_timeout_sec = 300
```

This file is only a candidate. Writing it does not grant authority. It must be a
normal file, not a symlink, and every relative root resolves under the
repository root according to policy validation rules.

## Step 6: Restart Codex and establish trust

Restart Codex from `RESEARCH_REPO` after changing the MCP command, arguments,
environment, hook, or project configuration. Trust the project when prompted,
then inspect and trust the exact global hook definition.

Confirm that `gpu-cluster-mcp` is visible. At this point an operational call may
return `policy_state: "bootstrap_pending"`; that proves the server started in
quarantine. If the tools are absent or the handshake fails, use the failure map
below instead of approving policy out of band.

## Step 7: Preview the exact candidate

Call:

```text
preview_policy_reload()
```

A valid first-policy response includes:

- `status: "preview"` and `validation: "pass"`;
- `approval_state: "bootstrap_pending"`;
- `activation_mode: "bootstrap"`;
- `active_hash: null` and a concrete `candidate_hash`;
- the complete `candidate_summary` and `diff_summary`; and
- a one-time `reload_token`.

Show the complete raw response to the human. Do not replace the candidate
summary, diff, or hashes with an agent-written summary. Ask the human to inspect
the actual policy file, especially its hosts and filesystem roots.

If validation fails, repair only the policy candidate and preview again. Do not
repoint the MCP registration or broaden unrelated permissions.

## Step 8: Obtain explicit approval and activate

Only after the human explicitly approves the exact previewed candidate, call:

```text
reload_policy(token=<reload_token>)
```

Codex must display the per-tool approval prompt. The prompt is a second
enforcement point, not a substitute for the human's review. Success requires
`status: "reloaded"`, `approval_state: "active"`, and an `active_hash` equal to
the previewed `candidate_hash`.

If the human rejects it, call
`reject_policy_reload(token=<reload_token>)`. Do not use
`gpu_mcp_doctor.py approve-policy --yes` as the normal installation path; that
command is an administrator recovery interface.

## Step 9: Verify the installation

First call a read-only operational tool and confirm that the result identifies
the expected repository and policy. Then run the focused installation test with
all deployment paths explicit:

```bash
/ABSOLUTE/GPU_MCP_SERVER_PYTHON \
  /ABSOLUTE/GPU_MCP_ROOT/installation_test.py \
  --repo /ABSOLUTE/RESEARCH_REPO \
  --mcp-root /ABSOLUTE/GPU_MCP_ROOT \
  --python /ABSOLUTE/GPU_MCP_SERVER_PYTHON \
  --json
```

`readiness: "ready"` with `proof_level: "codex_exec"` is the normal setup
proof. A live GPU launch proof is optional and must use an explicitly selected
host and harmless probe. The full battlefield suite is maintainer acceptance
testing, not a required step for every research repository.

## Failure map

| Symptom | Meaning and next action |
| --- | --- |
| Configured server file is missing | The confirmed runtime was not deployed or the local runbook is stale. Repair that deployment; do not choose another checkout implicitly. |
| `No module named mcp`, `fabric`, or `paramiko` | Install dependencies into `GPU_MCP_SERVER_PYTHON`. |
| Error says `FastMCP` was renamed to `MCPServer` | MCP SDK v2 is installed. Restore `mcp>=1.28,<2` until the project is migrated. |
| MCP tools are absent | Check project trust, project-config loading, exact command/args/cwd, and restart state. |
| `bootstrap_pending` | Startup succeeded; preview and activate the policy through MCP. |
| Policy is invalid or stale | Repair/preview `gpu-mcp.toml`; do not edit the runtime path. |
| SSH or remote path probe fails | Recheck bootstrap evidence, selected hosts, shared repo visibility, and `GPU_JOB_PYTHON`. |

## Non-negotiable rules

- Never infer the installed runtime from the checkout currently being edited.
- Never publish a site's `LOCAL_DEPLOYMENT.md` or copy its paths into portable
  examples.
- Never invent GPU hosts or treat bootstrap reachability as repository
  authorization.
- Never activate a policy without showing the complete raw preview to the human
  and receiving explicit approval.
- Never bypass Codex command rules with `--ignore-rules`.
- Never use raw remote commands as a workaround after GPU MCP refuses an
  operation.
- Treat `progress.md` as a journal, not as readiness evidence.
