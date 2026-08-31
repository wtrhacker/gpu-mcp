# How to Install GPU MCP for a Research Project

This guide is for an AI assistant (like Codex) that is helping a human set up
GPU access for their research code. GPU MCP is AI-native: the assistant does
the setup work, while the human reviews and approves the safety boundary.

**What is MCP?** MCP stands for Model Context Protocol. It is how an AI
assistant talks to tools. GPU MCP is a set of tools that let the AI run Python
jobs on remote GPU machines safely.

---

## What GPU MCP Does

GPU MCP is a safety layer that lets an AI agent run Python jobs on remote GPU
machines. It does four things:

1. **Runs** approved Python scripts on remote GPU hosts with safety guards.
2. **Checks** host reachability, GPU status, and policy compliance before and
during runs.
3. **Enforces** that only scripts from approved folders can execute, and only
writes to approved output locations.
4. **Coordinates** managed jobs and cooperative GPU reservations so agents can
track work without silently colliding.

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
defaults for the bootstrap preview; approval happens only after the raw,
validated preview is shown.

### Step 5: Write the Repo-Local Codex Config First

Before `gpu-mcp.toml` exists, write `.codex/config.toml` inside the research
repo. Point it at the exact future policy path:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/absolute/path/to/python"
args = [
  "/absolute/path/to/gpu_mcp_server.py",
  "--config",
  "/absolute/path/to/repo/gpu-mcp.toml",
]
cwd = "/absolute/path/to/repo"
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

# Codex must prompt the human at the activation boundary.
[mcp_servers.gpu-cluster-mcp.tools.reload_policy]
approval_mode = "prompt"
```

**Why config comes first:** The server can start safely before a usable policy
exists. A missing, invalid, unapproved, or changed-at-start policy puts it in
`bootstrap_pending` quarantine instead of making the MCP disappear. The
preview/activation tools remain available, while every GPU, SSH, process,
reservation, and managed-job operation is blocked by the server.

**Why repo-local config:** If you wrote this in global Codex config, every repo
would use the same GPU hosts and script paths. Each research repo needs its own
policy and MCP entry.

**Why absolute paths:** The server must know exactly which executable, server,
repo, and policy to use. Do not use relative paths or guess.

**Why `tool_timeout_sec` must be larger than `sync_timeout_sec`:** The client
must wait longer than the server's own job timeout.

### Step 6: Set Up the Policy Hook, Restart, and Establish Trust

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

Restart Codex from the research repo after editing either Codex config. Trust
the repo when Codex asks whether to load its project config, then use `/hooks`
to review and trust the global GPU MCP hook. A config edit does not mutate a
server that is already running; restart again whenever the MCP command, args,
or policy path changes.

At this point it is normal for operational tool calls to return
`policy_state: "bootstrap_pending"`. That is a live but quarantined MCP, not a
startup failure.

### Step 7: Write or Repair `gpu-mcp.toml`

Write the proposed policy to the repo root:

```toml
schema_version = 1
repo_root = "/shared/lab/my-research-repo"

nodes = ["gpu01.example.edu", "gpu02.example.edu"]

script_roots = ["."]
write_roots = [".", "/tmp/gpu_mcp_outputs"]
output_roots = [".gpu_mcp_logs"]

sync_timeout_sec = 300
```

Keep it small and explicit. This is a candidate, not an active policy.

The hook deliberately permits repair of that exact policy file while the
policy is invalid, unapproved, or stale. It also permits
`preview_policy_reload`, `reload_policy`, and `reject_policy_reload`. Do not
work around quarantine by editing unrelated files or calling raw remote
commands. If a policy edit remains invalid, call preview again to get its
validation error, repair only `gpu-mcp.toml`, and retry.

### Step 8: Preview and Show the Raw Candidate

Call `preview_policy_reload()`. It validates the file without activating it.
A valid initial response has:

- `status: "preview"` and `validation: "pass"`;
- `approval_state: "bootstrap_pending"`;
- `activation_mode: "bootstrap"`;
- `active_hash: null` and a concrete `candidate_hash`;
- the full `candidate_summary` and `diff_summary`; and
- a one-time `reload_token`.

Show the complete raw response to the human, including the candidate summary,
diff, both hashes, and token context. Do not replace it with your own summary.
Remind the human to inspect the actual `gpu-mcp.toml`, especially its nodes and
script/write/output roots.

If validation fails, there is no reload token and nothing can be activated.
Repair the policy and preview again.

### Step 9: Get Explicit Approval and Activate

Ask the human to explicitly approve the exact previewed candidate. Only after
they approve, call:

```text
reload_policy(token=<reload_token>)
```

Codex must show a tool-approval prompt because `reload_policy` is configured
with `approval_mode = "prompt"`. Do not call it before the human approves and
do not treat the prompt alone as approval. A successful response has
`status: "reloaded"`, `approval_state: "active"`, and an `active_hash` equal to
the previewed `candidate_hash`. Operations unlock in the same server process.

If the human rejects the candidate, call
`reject_policy_reload(token=<reload_token>)`. Quarantine remains in force; edit
only the policy after the human has oriented you to the next candidate.

Do not run `gpu_mcp_doctor.py approve-policy --yes` as the normal installation
path. It bypasses the MCP preview and prompted activation ceremony that makes
the initial policy review possible.

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
3. Show the complete raw preview, including the candidate summary, diff, and
   hashes, then ask the human to approve that exact candidate.
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
