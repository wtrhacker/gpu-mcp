# How to Install GPU MCP for a Research Project

This guide is for an AI assistant (like Codex) that is helping a human set up
GPU access for their research code.

**What is MCP?** MCP stands for Model Context Protocol. It is how an AI
assistant talks to tools. GPU MCP is a set of tools that let the AI run Python
jobs on remote GPU machines safely.

---

## What GPU MCP Does

GPU MCP is a safety layer that lets an AI agent run Python jobs on remote GPU
machines. For the current version (01-03) it does three things:

1. **Runs** approved Python scripts on remote GPU hosts with safety guards.
2. **Checks** host reachability, GPU status, and policy compliance before and
during runs.
3. **Enforces** that only scripts from approved folders can execute, and only
writes to approved output locations.

A future version (004) will add cooperative GPU reservations and managed job
tracking so multiple agents do not collide. This guide sets up the foundation
that 004 will build on.

The human keeps control. The agent cannot run arbitrary commands on GPU hosts.
It can only run approved Python scripts from approved folders.

---

## What You Need Before Starting

### Where the Scripts Live

The GPU MCP project lives somewhere on the control machine — for example, in
the human's home directory:

```text
~/gpu-mcp/
  gpu_mcp_server.py       # The MCP server
  gpu_mcp_doctor.py       # Install checker
  gpu_mcp_bootstrap.py    # SSH setup (human runs this)
  gpu_mcp_policy_hook.py  # Policy change detector
  test/                   # Test suite
```

If you do not know where this folder is, ask the human.

### The Human Must Run Bootstrap First

Do not start installing until the human has done this:

```bash
python ~/gpu-mcp/gpu_mcp_bootstrap.py --install gpu01.example.edu gpu02.example.edu
```

**What bootstrap does:** It creates an SSH key, installs it on the GPU hosts,
and checks which hosts are reachable. It writes the result to a file the AI can
read.

**Why the human must do this first:** SSH keys are the trust route to GPU
hosts. The AI should not create or install SSH keys silently. The human runs
this once, proves the dedicated key works, and then the AI can use the result.

If the human has not done this, stop and ask them to run it.

---

## Step-by-Step Installation

Copy `AI_native_installer/progress.template.md` to `progress.md` in the
research repo root now. Update it after every step.

### Step 1: Read the Bootstrap Result

Read `~/.cache/gpu-mcp/bootstrap_hosts.json`.

This file lists which GPU hosts were reached and which failed. Only use hosts
with `"status": "verified"`.

**Why:** Do not guess which hosts work. Use the human's bootstrap result as
evidence.

**If no hosts are verified:** Stop and tell the human the bootstrap failed.
Do not proceed with zero hosts.

### Step 2: Confirm the Research Repo Root

Ask the human: "What is the root folder of the research repo you want to use?"

**Why:** The AI might guess wrong (for example, a subfolder instead of the top
level). Wrong root means the GPU hosts will not find the scripts. Always
confirm with the human.

### Step 3: Propose GPU Hosts

Propose a subset of the verified bootstrap hosts as the `nodes` list.

**Why:** Not every reachable host should be used for this project. The human
may want to reserve some hosts for other work. Propose; do not decide alone.

Example proposal:

```toml
nodes = ["gpu01.example.edu", "gpu02.example.edu"]
```

### Step 4: Propose Script and Write Roots

Propose what the AI is allowed to run and where it is allowed to write. These
are safety boundaries.

| Setting | Usual proposal | What it means |
|---------|---------------|---------------|
| `script_roots` | `["."]` | The AI can run Python files anywhere in the repo. |
| `write_roots` | `[".", "/tmp/gpu_mcp_outputs"]` | The AI can write results to the repo or to a temp folder. |
| `output_roots` | `[".gpu_mcp_logs"]` | Where logs and outputs go. |

**Why:** If `script_roots` is too narrow, the AI cannot run experiments. If
`write_roots` is too broad, the AI could overwrite important files. Propose safe
defaults and ask the human to approve.

### Step 5: Ask the Human to Approve

Show the human a concrete `gpu-mcp.toml` with all values filled in. Ask for
explicit approval before writing it.

**Why:** Safety policy should not be set by the AI alone. The human must
understand and agree to which hosts, which scripts, and which write locations
are allowed.

### Step 6: Write `gpu-mcp.toml`

After the human approves, write the file to the repo root:

```toml
schema_version = 1
repo_root = "/shared/lab/my-research-repo"

nodes = ["gpu01.example.edu", "gpu02.example.edu"]

script_roots = ["."]
write_roots = [".", "/tmp/gpu_mcp_outputs"]
output_roots = [".gpu_mcp_logs"]

sync_timeout_sec = 300
```

Keep it small. Do not add fields the human did not approve.

**Why:** The server reads this file on every startup. A small, explicit policy
is easier to review and less likely to hide unsafe settings.

### Step 7: Approve the Policy

Run:

```bash
python ~/gpu-mcp/gpu_mcp_doctor.py approve-policy \
  --config /absolute/path/to/repo/gpu-mcp.toml --yes
```

**Why:** This records that the human has reviewed and approved the policy. The
MCP server will refuse to start until a policy is approved. This prevents the
AI from accidentally using an unsafe or unfinished config.

### Step 8: Write Repo-Local Codex Config

Write `.codex/config.toml` inside the research repo:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "python"
args = [
  "/absolute/path/to/gpu_mcp_server.py",
  "--config",
  "/absolute/path/to/repo/gpu-mcp.toml",
]
tool_timeout_sec = 360

# "approve" means the tool runs automatically without asking the human.
# Use this for operational tools so non-interactive runs (like codex exec) work.
[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.kill_gpu_process]
approval_mode = "approve"

[mcp_servers.gpu-cluster-mcp.tools.check_gpu_processes]
approval_mode = "approve"

# "prompt" means Codex asks the human before running the tool.
# Use this for policy changes so the AI cannot change safety boundaries silently.
[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
approval_mode = "prompt"

# 004 extension: add check_gpus, manage_gpu_job, list_gpu_reservations here.
# Read-only tools should be "approve". Lifecycle tools should be "approve".
```

**Why repo-local config:** If you wrote this in global Codex config, every
repo would use the same GPU hosts and script paths. That is dangerous. Each
research repo gets its own private config.

**Why `--config` with absolute path:** The server must know exactly which
policy to use. Relative paths or guessing would let the wrong policy slip in.

**Why `tool_timeout_sec` must be larger than `sync_timeout_sec`:** The server
has its own timeout (`sync_timeout_sec`). The Codex client must wait longer
than that, or the client will give up before the server does.

### Step 9: Set Up the Policy Hook

Add the GPU MCP companion hook once to the user's global Codex config:

```text
~/.codex/config.toml
```

Do not add hook blocks to the repo-local `.codex/config.toml`, and do not put a
research repo path in the global hook config. The hook discovers the current
repo by walking upward from the hook working directory to the nearest
`gpu-mcp.toml`; outside GPU MCP repos it exits quietly.

Append these global hook entries:

```toml
[[hooks.PreToolUse]]
matcher = "*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu_mcp_policy_hook.py"
timeout = 5
statusMessage = "Checking GPU MCP policy drift"

[[hooks.Stop]]
matcher = "*"

[[hooks.Stop.hooks]]
type = "command"
command = "/absolute/path/to/python /absolute/path/to/gpu_mcp_policy_hook.py"
timeout = 31536000
statusMessage = "Waiting for a managed GPU job event"
```

Restart Codex after editing config, then use `/hooks` to review and trust the
global GPU MCP hook.

**Why:** If the AI edits `gpu-mcp.toml` and forgets to ask the human, the
stale-policy hook blocks further tool calls. During active work, the companion
hook immediately surfaces a local job outcome or due status check. At turn end,
it waits for either event and continues the turn so the AI can call `status`.
The one-year timeout is an operational hook-runner watchdog, not a polling
maximum; raise it if this deployment intentionally suspends turns longer than a
year. The hook is scoped by the nearest current-repo policy.

### Step 10: Verify Raw Remote Command Blocking

Check that Codex will reject attempts to use raw SSH, `scp`, `rsync`, or spawn
a new Codex process.

**Why:** The MCP server only controls MCP tool calls. It cannot stop the agent
from running `ssh gpu01 rm -rf /` directly. The Codex client must block raw
remote commands so the MCP boundary is the only safe path to GPU hosts.

Verify by running a blocked probe with approvals disabled:

```bash
codex --ask-for-approval never exec "ssh gpu01 echo test"
```

This should fail or be rejected. If it succeeds, the safety rules are missing.

**Note:** `--ask-for-approval never` disables interactive approval for this
probe only. Never use `--ignore-rules`, which disables the safety layer entirely.

### Step 11: Run the Doctor

Run the full doctor check with JSON output:

```bash
python ~/gpu-mcp/gpu_mcp_doctor.py check \
  --config /absolute/path/to/repo/gpu-mcp.toml --json
```

**Why:** The doctor is the AI's evidence source. It checks config, SSH,
nvidia-smi, paths, and Codex setup. Do not guess whether things work. Read the
doctor JSON and treat it as the source of truth.

### Step 12: Run the Full Battlefield Suite

Run the real acceptance tests:

```bash
cd ~/gpu-mcp
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py
```

**Why:** Only a real run through `codex exec`, real SSH, and real GPU hosts
proves the install works. Everything before this is setup. This is the proof.

### Step 13: Mark Complete

If the battlefield suite passes and the global GPU MCP hook has been reviewed
and trusted in Codex, the install is done. Update `progress.md` with the
completed steps and any notes.

If any step fails, record the blocker in `progress.md` and stop. Do not skip
steps or pretend something works.

---

## What to Do If the Human Wants to Change Policy

If the human asks to change `gpu-mcp.toml` (add hosts, change roots, etc.):

1. Propose the exact diff.
2. Call the MCP tool `preview_policy_reload()` to validate the changed file.
3. Ask the human to approve.
4. If approved, call `reload_policy(token=<preview_token>)`.
5. If rejected, call `reject_policy_reload(token=<preview_token>)` and stop.

**Why:** Policy changes are safety-critical. Never edit `gpu-mcp.toml` and
continue working without the human explicitly approving the new policy.

---

## Rules You Must Follow

- Do not edit global Codex config with repo-specific paths.
- Do not reuse the legacy `gpu-cluster` MCP name. Use `gpu-cluster-mcp`.
- Do not use `--ignore-rules` for any install check. That flag disables the
  safety rules you are trying to verify.
- Do not run arbitrary user scripts during install.
- Do not invent hosts that are not in the bootstrap inventory.
- Treat `progress.md` as a journal, not proof. Doctor JSON and MCP probes are
  the real evidence.
