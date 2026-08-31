# GPU MCP

GPU MCP gives an AI coding agent a controlled way to run Python experiments on
lab GPUs.

Instead of giving an agent an unrestricted shell on a GPU machine, GPU MCP
exposes a small set of Model Context Protocol (MCP) tools. A human chooses the
GPU hosts, runnable code, and writable locations. The agent can then inspect
the cluster, reserve a GPU, launch a guarded Python job, follow its lifecycle,
and stop only a specific process it owns.

> [!IMPORTANT]
> GPU MCP is an experimental guardrail for trusted research environments. It is
> not a hostile-code sandbox, a cluster scheduler, or an isolation boundary
> between mutually untrusted users.

## Why it exists

AI agents are useful research partners, but ordinary remote shell access is a
poor interface for autonomous GPU work. It is too broad, has no durable notion
of job ownership, and makes it easy to collide with another experiment or lose
track of a long-running process.

GPU MCP turns that access into explicit, policy-constrained operations:

- inspect GPUs and the processes using them;
- reserve a device before launching work;
- run an approved Python file as a managed job;
- monitor, retry, finish, or stop that job through a durable handle;
- coordinate GPU use among agents running as the same user; and
- preview safety-relevant policy changes before a human approves them.

## AI-native setup

This project is installed *by an AI with a human in the loop*. The human does
not need to translate a long README into shell commands or hand-author MCP
configuration.

Point your coding agent to
[`AI_native_installer/INSTALL_FOR_AI.md`](AI_native_installer/INSTALL_FOR_AI.md)
and ask it to set up GPU MCP for the intended research repository. The guide
tells the agent how to inspect the environment, propose a repo-local policy,
write the required configuration, run diagnostics, and prove the setup.

The human remains responsible for the trust decisions:

- approve the exact GPU hosts and repository root;
- approve which Python files may run and where jobs may write;
- review policy changes and their safety-relevant diff;
- complete any interactive SSH credential or host-trust bootstrap; and
- review client hooks and rules that keep raw remote commands outside the
  allowed workflow.

Everything else should be carried out and checked by the agent. Each research
repository receives its own policy and MCP registration; GPU MCP itself can be
kept separately on the control machine.

### Activate a new repository

After the agent creates the repository policy and MCP registration, start or
restart Codex from that repository and trust the project. Ask the agent to call
`preview_policy_reload` and show you the complete raw policy summary, diff, and
hash. If they are correct, explicitly approve that exact candidate and accept
the `reload_policy` prompt. Normal GPU tools become available in the same
session after the reload reports the policy as active.

## How it fits together

```text
human-approved repo policy
            │
            ▼
AI coding agent ── MCP ── GPU MCP server on the control host
                              │
                              ├── policy and runtime guards
                              ├── reservations and managed-job state
                              └── dedicated SSH access
                                         │
                                         ▼
                                approved Linux GPU hosts
```

The current design assumes that the control host can reach the GPU hosts over
SSH, that all machines see the research repository at the same absolute path,
and that jobs run as the same authorized Unix user. Python files—not arbitrary
shell commands—are the execution unit.

## Safety model

GPU MCP is designed to reduce common agent mistakes while keeping the human in
control:

- A repo-local policy allowlists hosts, script roots, write roots, log roots,
  optional GPU models, and minimum free memory.
- Policy approval is bound to the file's hash. Later edits require a preview
  and explicit human-approved reload.
- Static and runtime guards reject common destructive APIs, subprocess and
  socket creation, and writes outside approved roots.
- Reservations, heartbeats, process fingerprints, and ownership checks reduce
  collisions and prevent broad or stale process signaling.
- A companion hook can surface policy drift and managed-job status events to
  the agent.

These controls do not make arbitrary Python trustworthy. A launched program
can still read anything available to its Unix account, and remote work retains
that account's normal OS permissions. Client-side rules are also required to
prevent bypassing MCP with raw `ssh`, `scp`, `rsync`, or another unrestricted
agent process.

## Where it fits today

GPU MCP is aimed at trusted labs with Linux NVIDIA hosts, `nvidia-smi`, direct
SSH connectivity, and a shared filesystem. It is not currently a backend for
SLURM, PBS, LSF, Kubernetes, or cloud GPU rental services, and it should not be
used for hostile multi-tenant execution.

The codebase includes contract tests, real Codex integration tests, and an
opt-in live GPU battlefield suite. The project is currently version `0.0.0`
and should be treated as experimental.

## Project map

- [`AI_native_installer/INSTALL_FOR_AI.md`](AI_native_installer/INSTALL_FOR_AI.md)
  is the setup procedure for an AI assistant.
- [`gpu_mcp_server.py`](gpu_mcp_server.py) provides the MCP tools and managed-job
  lifecycle.
- [`gpu_mcp_policy_hook.py`](gpu_mcp_policy_hook.py) reports policy drift and
  job events.
- [`gpu_mcp_doctor.py`](gpu_mcp_doctor.py) validates installation and policy
  state.
- [`ADR/`](ADR/) records the major design decisions.
- [`test/README.md`](test/README.md) explains the test layers and evidence
  requirements.

## License

No open-source license has been selected yet. Public availability does not by
itself grant permission to copy, modify, or redistribute this code.
