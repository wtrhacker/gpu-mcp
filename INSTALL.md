# Install GPU MCP Into Another Repo On This Machine

This guide is for an agent working in some other repo on this same machine.
It assumes the shared GPU MCP install already works here:

```text
/home/tingran/gpu-mcp
```

Do not edit or copy the MCP install. Do not put a repo policy file in
`/home/tingran/gpu-mcp`. To enable GPU MCP for a repo, add two files inside
that target repo:

```text
gpu-mcp.toml
.codex/config.toml
```

Then approve the repo policy and restart Codex from that repo.

## 1. Pick The Target Repo

Use the absolute path of the repo that should get GPU access:

```bash
REPO=/absolute/path/to/target-repo
test -d "$REPO" || exit 1
cd "$REPO" || exit 1
mkdir -p .codex
```

`gpu-mcp.toml` must live directly in `$REPO`, and its `repo_root` must exactly
match `$REPO`. It must be a normal file, not a symlink.

## 2. Write `gpu-mcp.toml`

Create `$REPO/gpu-mcp.toml`:

```toml
schema_version = 1
repo_root = "/absolute/path/to/target-repo"

nodes = ["GPU_HOST_1"]

script_roots = ["jobs"]
write_roots = ["results"]
output_roots = [".gpu_mcp_logs"]

allowed_gpu_names = []
min_free_memory_mib = 0
sync_timeout_sec = 300
```

Replace:

- `/absolute/path/to/target-repo` with the real repo path.
- `GPU_HOST_1` with a human-approved GPU host already provisioned for the
  shared MCP install. Do not edit `/home/tingran/gpu-mcp` or its bootstrap
  inventory from this repo-local install guide.

Keep the roots narrow for first install:

- `script_roots`: Python files the agent may launch.
- `write_roots`: durable result directories scripts may write to.
- `output_roots`: MCP stdout/stderr log directories.

The root categories are not optional: every repo policy needs `script_roots`,
`write_roots`, and `output_roots`. The literal names `jobs`, `results`, and
`.gpu_mcp_logs` are examples. In an existing repo, use existing repo-local
script/result directories when that is the safer fit.

If you use the example policy above, create those directories now:

```bash
mkdir -p jobs results .gpu_mcp_logs
```

## 3. Approve The Policy

After the human approves the policy contents, run:

```bash
/home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_doctor.py \
  approve-policy \
  --config "$REPO/gpu-mcp.toml" \
  --yes
```

This is required. The MCP server refuses unapproved or stale repo policies.

## 4. Write Repo-Local Codex Config

Create or merge this into `$REPO/.codex/config.toml`:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/home/tingran/miniconda3/bin/python"
args = [
  "/home/tingran/gpu-mcp/gpu_mcp_server.py",
  "--config",
  "/absolute/path/to/target-repo/gpu-mcp.toml",
]
cwd = "/absolute/path/to/target-repo"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 360

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

Replace both `/absolute/path/to/target-repo` occurrences.

Important:

- This config is repo-local. Do not add this MCP server entry to
  `~/.codex/config.toml`.
- Use the MCP name `gpu-cluster-mcp`.
- Remove or replace old repo-local GPU MCP entries such as
  `[mcp_servers.gpu-cluster]` or entries pointing at old test repos.
- Keep `tool_timeout_sec` greater than `sync_timeout_sec`.

## 5. Run The Install Test

After writing `.codex/config.toml`, run:

```bash
/home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/installation_test.py \
  --repo "$REPO"
```

This checks the shared MCP install files, the target repo policy, policy
approval freshness, repo-local Codex MCP config, timeout alignment, and the
server Python dependencies. It then starts a fresh `codex exec -C "$REPO"`
session and asks it to call `gpu-cluster-mcp/check_gpus`.

The install test does not use `codex mcp list`. A passing install test requires
the actual repo-local MCP tool call to work from Codex.

`readiness=ready` with `proof_level=codex_exec` means the repo is ready to use
the MCP.

## 6. Live GPU Launch Proof

When you want a real launch proof, run:

```bash
/home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/installation_test.py \
  --repo "$REPO" \
  --live-gpu \
  --host GPU_HOST_1 \
  --gpu-index 0
```

This writes a tiny temporary probe script under the first configured
`script_roots` directory, runs it through `gpu-cluster-mcp/run_python_on_gpu`,
checks it with `manage_gpu_job`, and requires the result to show the selected
`CUDA_VISIBLE_DEVICES` value and GPU framework use.

## 7. Confirm The Global Hook

The global GPU MCP companion hook is installed on this machine. It runs:

```text
PreToolUse:  /usr/bin/env GPU_MCP_HOOK_REMINDER_MODE=additionalContext /home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_policy_hook.py
PostToolUse: /home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_policy_hook.py
```

Do not add repo-local hook blocks, and do not put target-repo paths in the
global hook command. The hook discovers the current repo from `gpu-mcp.toml`.

## 8. Restart Codex From The Target Repo

Start a fresh Codex session from the target repo:

```bash
cd "$REPO"
codex
```

The repo should now have the `gpu-cluster-mcp` tools available. The MCP server
and hook provide their own runtime guidance; this file is only for installing
the MCP into a repo.

If a fresh session refuses because the policy is unapproved, rerun
`approve-policy` for `$REPO/gpu-mcp.toml`. If an already-running session refuses
because the policy became stale after editing `gpu-mcp.toml`, use
`preview_policy_reload`, show the preview to the human, and call
`reload_policy` only after explicit approval.

## 9. Maintainer Battlefield Proof

The per-repo install test above is the normal readiness check. The full
battlefield suite is maintainer acceptance evidence for the shared MCP install:

```bash
cd /home/tingran/gpu-mcp
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py
```
