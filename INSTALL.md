# Install GPU MCP Into Another Repo On This Machine

This guide is for an agent working in another repo on this machine. The shared
GPU MCP install already lives at:

```text
/home/tingran/gpu-mcp
```

Do not edit or copy that install. A target repo gets its own MCP entry and
safety policy. Configure the MCP first, then create and activate the policy
through GPU MCP's human-reviewed bootstrap flow.

## 1. Pick the Target Repo

Use the absolute path of the repo that should get GPU access:

```bash
REPO=/absolute/path/to/target-repo
test -d "$REPO" || exit 1
cd "$REPO" || exit 1
mkdir -p .codex
```

The future `gpu-mcp.toml` must live directly in `$REPO`, and its `repo_root`
must exactly match `$REPO`. It must be a normal file, not a symlink.

## 2. Configure the Repo-Local MCP First

Create or merge this into `$REPO/.codex/config.toml` before writing the policy:

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

Replace both `/absolute/path/to/target-repo` occurrences. Keep
`tool_timeout_sec` greater than the policy's `sync_timeout_sec`.

This entry must remain repo-local. Do not add it to `~/.codex/config.toml`, do
not add repo-local hook blocks, and do not reuse an old MCP name such as
`gpu-cluster`.

## 3. Restart Codex and Establish Trust

Start a fresh session from the target repo:

```bash
cd "$REPO"
codex
```

Trust the repo when Codex asks whether to load its project config. If the
global GPU MCP hook is new or changed, use `/hooks` to inspect and trust it.
Changes to `.codex/config.toml` do not update an already-running MCP process;
restart Codex whenever the command, args, cwd, or policy path changes.

Because `gpu-mcp.toml` does not exist yet, the server should remain live in
`bootstrap_pending` quarantine. Calls to GPU, SSH, process, reservation, and
managed-job tools must return a refusal. `preview_policy_reload`,
`reload_policy`, and `reject_policy_reload` remain available. If no MCP tools
are visible at all, diagnose project-config loading, trust, paths, and restart
state; do not use policy approval as a workaround for a client configuration
failure.

## 4. Write the Candidate Policy

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

Replace the repo path and propose only GPU hosts from the human-provisioned
bootstrap inventory. Do not edit `/home/tingran/gpu-mcp` or its inventory.

Keep roots narrow:

- `script_roots` contains Python files the agent may launch.
- `write_roots` contains durable result directories scripts may write to.
- `output_roots` contains MCP stdout/stderr log directories.

If using the example roots, create them:

```bash
mkdir -p jobs results .gpu_mcp_logs
```

A missing or invalid policy keeps the server quarantined. The global hook
allows repair of the exact `gpu-mcp.toml` and allows the three policy recovery
tools, while continuing to block unrelated work. Call `preview_policy_reload`
after each repair. Do not edit Codex config merely to fix a TOML validation
error, and do not restart for an ordinary policy repair.

## 5. Preview, Obtain Human Approval, and Activate

Call the MCP tool `preview_policy_reload()`. It validates the candidate but
does not activate it. For an initial policy, require a raw response containing:

- `status: "preview"`, `validation: "pass"`, and
  `approval_state: "bootstrap_pending"`;
- `activation_mode: "bootstrap"`;
- `active_hash: null` and a concrete `candidate_hash`;
- the complete `candidate_summary` and `diff_summary`; and
- a one-time `reload_token`.

Show the complete raw preview to the human. Do not substitute an agent summary
for the candidate summary, diff, or hashes. Ask the human to inspect the actual
file and explicitly approve that exact preview.

Only after explicit approval, call:

```text
reload_policy(token=<reload_token>)
```

Codex must prompt because `reload_policy` has `approval_mode = "prompt"`. The
prompt is a second enforcement point, not a substitute for explicit approval.
A successful result has `status: "reloaded"`, `approval_state: "active"`, and
an `active_hash` equal to the previewed `candidate_hash`. Operational tools
unlock in the same server process.

If the human rejects the candidate, call `reject_policy_reload` with the token.
The server stays quarantined; edit only the policy after the human re-orients
you to a new candidate.

Do not use `gpu_mcp_doctor.py approve-policy --yes` for normal installation.
That bypasses the MCP preview and prompted activation ceremony.

## 6. Run the Install Test

After activation, run:

```bash
/home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/installation_test.py \
  --repo "$REPO"
```

This checks the shared install, target policy, approval freshness, repo-local
MCP config, timeout alignment, and server dependencies. It then starts a fresh
`codex exec -C "$REPO"` session and calls `gpu-cluster-mcp/check_gpus`.

`readiness=ready` with `proof_level=codex_exec` means the repo is ready. The
test deliberately exercises the real repo-local MCP instead of relying on
`codex mcp list`.

## 7. Run a Live GPU Launch Proof

When a real launch is appropriate, run:

```bash
/home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/installation_test.py \
  --repo "$REPO" \
  --live-gpu \
  --host GPU_HOST_1 \
  --gpu-index 0
```

This creates a tiny probe under the first `script_roots` directory, launches it
through `run_python_on_gpu`, tracks it with `manage_gpu_job`, and verifies the
selected `CUDA_VISIBLE_DEVICES` value and GPU framework use.

## 8. Confirm the Global Hook

The global companion hook on this machine runs:

```text
PreToolUse:  /usr/bin/env GPU_MCP_HOOK_REMINDER_MODE=additionalContext /home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_policy_hook.py
PostToolUse: /home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_policy_hook.py
Stop:        /home/tingran/miniconda3/bin/python /home/tingran/gpu-mcp/gpu_mcp_policy_hook.py  (timeout: 31536000s operational watchdog)
```

Do not add target-repo paths to the global commands. The hook finds the nearest
`gpu-mcp.toml`. If the hook config changes, restart Codex and review it again
with `/hooks` before relying on it.

## 9. Change Policy Safely Later

Editing an active `gpu-mcp.toml` makes the running policy stale. Operational
tools remain blocked until the agent calls `preview_policy_reload`, shows the
complete raw candidate summary, diff, and hashes to the human, receives
explicit approval, and calls the prompted `reload_policy` with that preview's
token. Rejection consumes the token and leaves the old policy active.

A fresh server that sees a changed-at-start policy uses the same
`bootstrap_pending` flow, with no approved baseline loaded into the process.

## 10. Maintainer Battlefield Proof

The per-repo install test is the normal readiness check. The full battlefield
suite is maintainer acceptance evidence for the shared MCP install:

```bash
cd /home/tingran/gpu-mcp
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py
```
