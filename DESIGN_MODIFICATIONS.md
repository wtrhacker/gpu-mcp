# GPU MCP Public-Ready Plan

This is the working plan for turning the current site-specific GPU MCP server
into a small, publishable project that other labs can install with help from an
AI agent. The previous design note was copied to
`DESIGN_MODIFICATIONS.original.md` before this file was edited.

The plan is intentionally narrow. The goal is deterministic setup for trusted
lab GPU fleets, not a general HPC scheduler or a broad remote execution system.

Guiding rule: keep the current design, remove site-specific and repo-specific
assumptions from source code, document the safety model honestly, and improve
the install path that matters for normal lab use.

## Current Plan Status

- [x] Preserve the original design note.
- [ ] Add repo-local `gpu-mcp.toml` policy loading.
- [ ] Add a publishable example config.
- [ ] Make SSH bootstrap a human-first prerequisite.
- [ ] Make bootstrap write a user-level reachable-host inventory.
- [ ] Add an AI-facing doctor harness.
- [ ] Add an AI-facing install checklist template.
- [ ] Update README into public setup documentation.
- [ ] Keep the current Python-only guarded execution model.
- [ ] Keep tests passing after each phase.
- [x] Prove repo-local Codex MCP config with two `codex exec` fixture repos.

## Implementation Phases

### Phase 1: Config Model

Deliverables:

- `gpu_mcp_config.py` or equivalent config-loading helpers.
- `examples/gpu-mcp.example.toml`.
- tests for config discovery, validation, path resolution, and failure modes.

Acceptance criteria:

- Server requires `--config /absolute/path/to/gpu-mcp.toml`.
- Missing, relative, or invalid `--config` paths fail with a clear startup
  error.
- No MIT node list, server-source repo root, or fixed GPU model list is used as
  silent fallback policy. This only needs a small startup/doctor check, not a
  `codex exec` policy test.
- Existing safety tests still pass after being adapted to the config model.

### Phase 2: Human-First SSH Bootstrap

Deliverables:

- `gpu_mcp_bootstrap_ssh.py` no longer imports the server's hard-coded `NODES`
  as the normal path.
- Bootstrap requires explicit hosts or `--hosts-file`.
- Bootstrap writes a machine-readable user-level report/inventory.

Proposed inventory path:

```text
~/.cache/gpu-mcp/bootstrap_hosts.json
```

Proposed inventory shape:

```json
{
  "generated_at": "2026-05-20T15:10:00-04:00",
  "ssh_user": "alice",
  "key_path": "/home/alice/.ssh/gpu_mcp_key",
  "hosts": [
    {
      "host": "gpu01.example.edu",
      "status": "verified",
      "remote_hostname": "gpu01",
      "error": ""
    },
    {
      "host": "gpu02.example.edu",
      "status": "failed",
      "remote_hostname": "",
      "error": "connection timed out"
    }
  ]
}
```

Acceptance criteria:

- A human can run bootstrap before involving the installer AI.
- Failed or offline hosts are recorded instead of becoming ambiguous install
  failures later.
- The installer AI can read the global inventory and propose `nodes = [...]`
  from verified hosts only.
- The AI still asks the human to approve the final node list before writing
  `gpu-mcp.toml`.
- The global inventory is evidence of SSH reachability, not permission. The
  repo-local `gpu-mcp.toml` remains the host allowlist policy.

### Phase 3: AI Installer Harness

Deliverables:

- `AI_native_installer/INSTALL_FOR_AI.md`.
- `AI_native_installer/progress.template.md`.
- `AI_native_installer/gpu_mcp_doctor.py`.
- Active Codex raw remote command block check.

The doctor script is primarily for the installer AI, with human-readable output
as a secondary benefit. It should expose deterministic checks so the AI does not
infer success from weak signals.

Doctor CLI details live in `AI_native_installer/doctor_cli.md`.

Acceptance criteria:

- Doctor can validate config parsing and path policy without network access.
- Doctor can validate dedicated-key SSH for configured hosts.
- Doctor can validate `nvidia-smi` and remote repo visibility on reachable
  hosts.
- Doctor can validate remote `realpath(repo_root)`, remote Python executable,
  probe script hash, working directory, and minimal imports.
- Doctor can print a repo-local Codex MCP config snippet or validate an
  installed repo-local one with a real `codex exec` probe.
- Doctor can verify global Codex execpolicy prompt rules and blocked
  `codex exec` probes for raw remote-access commands such as `ssh`, `scp`,
  `sftp`, and `rsync`.
- Doctor rejects raw-command probe invocations that include `--ignore-rules`,
  because that flag disables execpolicy.
- Doctor can verify that client `tool_timeout_sec` is larger than
  `sync_timeout_sec` by a small margin.
- Doctor does not silently install SSH keys, edit global Codex config, broaden
  write roots, or run arbitrary user scripts.

`progress.md` is created from `AI_native_installer/progress.template.md`. The
installer AI must update it after every completed step or blocker.
The file is not proof and is not a source of security policy; it is only an
install journal and resume point. Doctor JSON and MCP probe outputs are the
canonical evidence.

Doctor text output can be friendlier, but it should be derived from the same
check objects as JSON so the AI and human see the same facts.

### Phase 4: Public README

Deliverables:

- Rewrite `GPU_MCP_README.md` into a public setup guide.
- Keep a clear distinction between optional local smoke checks and required
  remote end-to-end acceptance. Local-only MCP probes do not prove install
  success.
- Explain the safety model without overselling it.

Acceptance criteria:

- README tells the human to bootstrap SSH first with explicit hostnames.
- README tells the installer AI to use `INSTALL_FOR_AI.md` and `progress.md`.
- README distinguishes direct SSH success from MCP success.
- README says this is for trusted lab GPU fleets with shared filesystems.
- README says this is Python-only execution.

### Phase 5: Final Verification

Human user will launch codex on a new machine and try install from scratch AI-natively.

## Target Scope

This project targets trusted lab GPU fleets.

Expected environment:

1. A control host can SSH to named Linux GPU hosts.
2. The same repo path is visible on the control host and GPU hosts.
3. Users are allowed to run jobs directly on the GPU hosts.
4. Jobs are Python scripts.

This fits many small academic lab GPU pools. It does not target scheduler-mandated clusters where users must submit through SLURM/PBS/LSF.

Keep `localhost` support for install checks and debugging, but do not present the project as a single-workstation tool.

## Non-Goals For First Public Version

Do not add these now:

- SLURM/PBS/LSF backend.
- Generic shell/R/Bash script execution.
- Complex hook system.
- Same-user multi-agent coordination logic.
- A database or daemon.

These can be revisited later, but the first public version should stay close to the current working system. Multi-user Unix safety is still in scope: the current cancellation flow refuses to signal processes whose owner does not match the configured MCP user.

## Current Problem

The current server works, but core site-specific and repo-specific policy is hard-coded in `gpu_mcp_server.py`.

Examples:

- GPU host list is in `NODES`.
- GPU model filtering is in `RTX4090_HOSTS`.
- approved script roots are based on `REPO_ROOT`.
- approved write/output roots are hard-coded plus environment variables.
- `REPO_ROOT` currently means the directory containing `gpu_mcp_server.py`.

That means another lab must edit Python source before using the repo. It also means the MCP server code is coupled to the research repo whose scripts it is allowed to run. For a public project, both need to be fixed.

## Repo-Specific Policy Model

GPU MCP should be installed once, but enabled per research repo.

The server code can live somewhere stable, for example a cloned `gpu-mcp` repo or a user-local install directory. Each research repo that wants GPU access should contain its own private policy file:

```text
gpu-mcp.toml
```

This file should not be committed by default. Publish only an example:

```text
examples/gpu-mcp.example.toml
```

The server should refuse to start unless it can find an approved repo-specific `gpu-mcp.toml`. This is intentional: GPU access should be enabled per repo, not globally inherited by every project an AI agent opens.

In the public design, `repo_root` should mean the research repo being managed, not the directory containing the MCP server source file. All script roots, write roots, output roots, local working directories, and remote `cd` commands should resolve against this configured `repo_root`.

Minimal first-version config:

```toml
repo_root = "/shared/lab/my-research-repo"

nodes = ["gpu01.example.edu", "gpu02.example.edu"]

script_roots = [
  "."
]

write_roots = [
  ".",
  "/tmp/gpu_mcp_outputs"
]
```

Here `"."` means `repo_root`. This keeps the config portable when the same absolute repo path is mounted on the control host and GPU hosts.

Optional fields:

```toml
output_roots = [
  ".gpu_mcp_logs",
  "/tmp/gpu_mcp_logs"
]
allowed_gpu_names = ["NVIDIA GeForce RTX 4090"]
min_free_memory_mib = 16000
sync_timeout_sec = 300
```

Keep this small. Do not expose internal tuning fields unless they are actually needed.

`sync_timeout_sec` should be chosen during install. Short interactive probes
should use a small value. Long simulations should usually run asynchronously and
write logs under `output_roots`; if the user wants synchronous long jobs, the
installer AI should ask before setting a large timeout. The MCP client
`tool_timeout_sec` must be set higher than `sync_timeout_sec` by a small margin
so the server's own timeout is the one observed.

Do not move every current environment override into `gpu-mcp.toml`. For the first public version, keep SSH user and Python executable behavior close to the current design: default to the user running the MCP server and the Python executable used to launch the MCP server, with existing environment overrides only when needed.

### What This Replaces

`gpu-mcp.toml` replaces these hard-coded values:

- `REPO_ROOT` as "server source directory"
- `NODES`
- `RTX4090_HOSTS` / fixed GPU-name assumptions
- `APPROVED_SCRIPT_ROOTS`
- `APPROVED_OUTPUT_ROOTS`
- parts of `APPROVED_WRITE_ROOTS`

It does not replace the Codex sandbox config. Codex config is client-side policy; `gpu-mcp.toml` is server-side repo/cluster policy. Codex profiles control how often the human is asked before tool calls. `gpu-mcp.toml` controls what the MCP server will allow at all. A less-interactive Codex profile can reduce approval prompts, but it should not let the MCP run outside the configured hosts, script roots, or write roots. This distinction should be explained in comments in the example config.

For hosts, the repo policy is a thinning layer over the user-level bootstrap
inventory:

```text
user-level verified SSH hosts -> human-approved repo subset -> gpu-mcp.toml nodes
```

The server must treat only `gpu-mcp.toml` as permission. The bootstrap inventory
never grants access by itself.

### Repo-Local Codex Client Config

Repo-specific MCP registration must not live in global Codex config. Global
config may contain general defaults, profiles, plugins, and reusable hook
support, but it must not contain a fixed research repo path, fixed
`gpu-mcp.toml` path, repo host list, or repo write roots.

The supported first-version Codex mechanism is repo-local config:

```text
<research-repo>/.codex/config.toml
```

The installer AI should install or update GPU MCP registration in that
repo-local Codex config. The entry should start the server with:

```text
--config /absolute/path/to/that/repo/gpu-mcp.toml
```

The new implementation uses one stable MCP server name everywhere:
`gpu-cluster-mcp`. Every repo-local `.codex/config.toml` uses that exact name;
repo-specific behavior comes only from the `--config` path. Do not create
per-repo names such as `gpu-cluster-mcp-a`, and do not reuse the legacy
`gpu-cluster` entry during migration or testing.

The required tool approval mode for noninteractive `codex exec` probes is:

```toml
[mcp_servers.gpu-cluster-mcp.tools.run_python_on_gpu]
approval_mode = "approve"
```

The doctor should flag a global `gpu-cluster` MCP entry pointing at a specific
repo as legacy or unsafe for multi-repo use.

Important observed behavior: `codex exec -C <repo>` loaded and used
`<repo>/.codex/config.toml` in the two-repo probe, but
`codex mcp list -C <repo>` did not show the repo-local server. Therefore doctor
must validate repo-local MCP with an actual `codex exec` tool call, not just
`codex mcp list`.

See `test/support/CODEX_REPO_LOCAL_MCP_NOTES.md` for the repo-local MCP
experiment record and `test/support/CODEX_EXECPOLICY_NOTES.md` for the
execpolicy command-blocking probe.

### Global Codex Raw Remote Command Prompt Rules

The MCP server can only police MCP tool calls and scripts launched through MCP.
It cannot stop the outer agent from using raw shell commands such as `ssh`,
`scp`, `sftp`, or `rsync`. For v1, the answer is deliberately simple: the
installer verifies Codex execpolicy prompt rules for raw remote-access commands
and verifies that GPU-MCP automation runs with approvals disabled.

The global rules should cover at least:

```python
prefix_rule(pattern=["ssh"], decision="prompt")
prefix_rule(pattern=["scp"], decision="prompt")
prefix_rule(pattern=["sftp"], decision="prompt")
prefix_rule(pattern=["rsync"], decision="prompt")
```

It should also cover obvious raw-SSH forms where Codex supports that:

```text
/usr/bin/ssh
command ssh
env ssh
```

This is Codex client configuration, not MCP server security. The project targets
Codex, so this is acceptable for v1. Do not make the ban repo- or host-specific:
normal Codex work should not use raw remote-access commands at all.

Important limitation: on the tested Codex version, execpolicy did not accept a
literal `deny` decision. `decision="prompt"` becomes an effective block for
noninteractive `codex exec` only when approvals are disabled. Interactive Codex
sessions with approvals enabled may still ask the human.

`--ignore-rules` disables execpolicy and must not be used by installer or
doctor validation probes except in a deliberate proof experiment. Doctor must
validate that raw-command probes run with approvals disabled and without
`--ignore-rules`.

The phase matters. Human bootstrap and doctor probes may use controlled SSH
internally. Normal GPU-MCP-enabled agent work should not use raw remote-access
commands directly.

Install phase:

- human bootstrap may install and verify the dedicated SSH key;
- doctor may run controlled SSH, `nvidia-smi`, path, and Python probes;
- normal GPU-MCP automation uses MCP tools with approvals disabled, so
  prompt-required raw remote-access shell commands are rejected.

### Server Startup

The MCP server needs an unambiguous way to find the repo-specific policy file.

Supported first-version startup:

```bash
python /path/to/gpu_mcp_server.py --config /absolute/path/to/research/repo/gpu-mcp.toml
```

The installer AI writes this into repo-local MCP client config. The human
approves the policy values inside `gpu-mcp.toml`, not the command-line
plumbing.

The server should validate on startup:

- `gpu-mcp.toml` exists;
- `repo_root` exists;
- `repo_root` in the config matches the directory containing `gpu-mcp.toml`;
- configured script/write/output roots resolve under allowed locations;
- every configured root is valid on the control host;
- at least one configured GPU host is reachable during verification, not necessarily during startup.

Do not read config from `cwd`. Do not read `GPU_MCP_CONFIG`. Do not silently
fall back to a built-in MIT node list or to the server source directory as the
repo root.

## AI-Native Setup

The user should not have to manually write most of this, but the SSH bootstrap
step should be human-first. It creates the trust route that lets the MCP reach
GPU hosts, so it should happen before an installer AI writes repo policy or
declares anything installed.

Human prerequisite:

```bash
python gpu_mcp_bootstrap_ssh.py gpu01.example.edu gpu02.example.edu gpu03.example.edu
```

or:

```bash
python gpu_mcp_bootstrap_ssh.py --hosts-file gpu-hosts.txt
```

The bootstrap result should become the installer AI's source of candidate
reachable hosts. This inventory is user-level evidence that the dedicated SSH
key works; it is not repo permission. Hosts that fail bootstrap should remain in
the report, but should not be proposed in `gpu-mcp.toml` unless the human
explicitly asks to retry or include them.

The README should tell an AI agent to:

1. Read `INSTALL_FOR_AI.md`.
2. Create or update `progress.md` from `examples/progress.md`.
3. Confirm the human has already run SSH bootstrap with explicit hostnames or a
   hosts file.
4. Read `~/.cache/gpu-mcp/bootstrap_hosts.json`.
5. Identify the current research repo path and ask the user to confirm it as
   `repo_root`.
6. Install or locate the GPU MCP server code outside the research repo if it is
   not already available.
7. Propose `nodes` as a repo-specific subset of verified bootstrap-inventory
   hosts only.
8. Probe verified hosts with harmless `nvidia-smi` checks where dedicated-key
   SSH access works.
9. Fill in discovered GPU names and memory only when useful; do not force the
   user to hand-enter hardware details.
10. Propose `script_roots`, usually `"."`.
11. Propose `write_roots`, usually `"."`, `.gpu_mcp_logs`, and explicit result
    directories the user names.
12. Ask the human to approve repo root, hosts, script roots, and write roots
    before writing the config.
13. Create a repo-local `gpu-mcp.toml` in the confirmed research repo.
14. Configure repo-local MCP client config so this repo starts the server with
    `--config /absolute/path/to/gpu-mcp.toml`.
15. Optionally run a local MCP smoke probe to catch configuration mistakes.
16. Run required remote acceptance through `codex exec` and the real installed
    MCP on a non-local host verified by bootstrap.
17. Run a required policy-rejection probe, such as a job that attempts to write
    outside approved write roots.
18. Update `progress.md` after every completed step or blocker.

Safety-relevant values should be approved by the human. Non-critical defaults should be automatic.

### Agent Asking Rules

The setup should be AI-native, but not AI-autonomous for safety policy.

The agent may infer:

- the current repo path;
- the server install path;
- candidate GPU hostnames from the user-level bootstrap inventory;
- GPU names and memory from harmless `nvidia-smi` probes on verified hosts;
- default log paths under the repo or `/tmp`.

The agent must ask the human to approve:

- the final `repo_root`;
- the allowed GPU host list proposed as a subset of verified bootstrap-inventory
  hosts;
- every `script_root`;
- every durable `write_root`;
- any result directory outside the repo;
- any broad filesystem permission in the Codex/client sandbox config.

The agent should not ask the human to hand-fill low-risk mechanical values. It
should propose a concrete config, explain the safety-relevant parts briefly, and
ask for approval before writing `gpu-mcp.toml`.

The agent should not invent hosts that are not in the bootstrap inventory. If no
non-local host is verified in the inventory, the install is blocked until the
human runs or reruns bootstrap.

### Repo Root vs Script Roots

`repo_root` is the base directory for one research repo. The server uses it as the working directory locally and remotely.

`script_roots` are the subdirectories where the MCP is allowed to run Python scripts from. They are resolved relative to `repo_root` unless written as absolute paths.

For the simple first version, use:

```toml
repo_root = "/shared/lab/my-research-repo"
script_roots = ["."]
```

That means: this MCP may run Python files anywhere under this research repo.

Use narrower `script_roots` only if the user wants to restrict execution further:

```toml
repo_root = "/shared/lab/my-research-repo"
script_roots = ["gpu_jobs", "experiments"]
```

That means: this MCP may run Python files under `/shared/lab/my-research-repo/gpu_jobs` and `/shared/lab/my-research-repo/experiments`, but not arbitrary Python files elsewhere in the repo.

## SSH Key Handling

Keep the current dedicated-key bootstrap model, but make it the first install
step owned by the human.

Default key:

```text
~/.ssh/gpu_mcp_key
```

The bootstrap script can generate and install this key. Users should not need to
put an SSH key path in config unless they have a special setup.

For the public version, bootstrap should take explicit hostnames or
`--hosts-file`. It should not normally import `NODES` from the MCP server. The
configured MCP node list should flow in the other direction:

```text
human host list -> user-level bootstrap inventory -> AI-proposed repo subset -> gpu-mcp.toml
```

This removes a circular dependency where the server has to know the cluster
before the installer can test reachability.

## Python-Only Execution

Keep execution Python-only for now.

Reason:

- the current safety checks understand Python;
- AST scanning and runtime audit hooks are Python-specific;
- generic script execution would need a different safety model.

The public docs should say this clearly. Do not rename the tool to generic `run_script` unless non-Python execution is actually supported.

Also say what this is not: AST scanning and Python audit hooks are guardrails
for trusted lab scripts and common AI-generated mistakes. They are not a
hostile-code sandbox for arbitrary Python packages, native extensions, or a
determined malicious author.

Possible public name:

```text
run_python_on_gpu
```

or:

```text
run_python_script_on_gpu
```

Either is fine. The important part is not to imply arbitrary scripts are guarded.

## Process Cancellation

Current behavior:

- inspect target process by host and PID;
- require owner to match `GPU_MCP_USER`;
- return process details and a fingerprint;
- require a second call with the fingerprint;
- re-inspect before signaling;
- allow `TERM` or `KILL`.

This is much safer than blind process killing.

However, it can still signal any process owned by the configured user on an allowed host. That includes jobs launched manually outside the MCP.

For the first public version, document this honestly.

Recommended default wording:

> `kill_gpu_process` can signal a process owned by the configured Unix user on an allowed host, after inspection and fingerprint confirmation. It is not restricted to jobs launched by this MCP.

Do not add a launch registry yet. For now, assume one active agent under the Unix account. Put that assumption in the README.

Later, if needed, add a simple MCP-launched-only registry. Do not implement it in the first cleanup pass.

## Same-User Multi-Agent Assumption

First public version assumption:

> Only one AI agent is expected to actively manage jobs under a given Unix account at a time.

This avoids adding coordination machinery now.

If a user runs multiple agents in different terminal/byobu sessions under the same Unix account, they are responsible for avoiding conflicting cancellation decisions.

This is separate from multi-user safety. The current process cancellation design already protects other Unix users by requiring process ownership to match `GPU_MCP_USER`. This assumption should be visible in the README and SECURITY docs.

## Performance Change

Improve `check_gpus` before publication.

Current issue:

- hosts are checked serially;
- slow or offline hosts delay the whole response;
- multiple samples add more latency.

Near-term change:

- probe hosts concurrently with bounded fanout;
- add a `hosts` argument to `check_gpus`;
- keep `samples` because repeated `nvidia-smi` reads can smooth utilization; use a default sample of 3.

Do not add cache configuration.


## Documentation Changes

Replace the current single setup README with:

- `README.md`
- `docs/AI_INSTALL.md`
- `docs/CONFIG.md`
- `docs/SECURITY.md`
- `docs/OPERATIONS.md`
- `examples/gpu-mcp.example.toml`

Keep language direct. The docs should answer:

- What kind of cluster is supported?
- What assumptions must be true?
- What does the MCP do?
- What does it refuse to do?
- What can an AI agent safely infer?
- What must the agent ask the human to approve?
- How do we verify local and remote operation?

## Safety Tests

Keep `test_gpu_mcp_server_safety.py` and make it part of the public story.

It should cover the behavior already present:

- forbidden imports;
- forbidden calls;
- dynamic execution rejection;
- write-root enforcement;
- output path enforcement;
- host allowlist behavior;
- process fingerprint behavior;
- benign Python script execution.

Do not expand the test suite into unrelated infrastructure tests before the repo is cleaned up.
