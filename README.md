# GPU MCP

GPU MCP lets an AI assistant run Python experiments on lab GPUs without giving
it an unrestricted remote terminal.

MCP (Model Context Protocol) is simply a way to give an AI named functions. You
talk to the AI normally; it uses these functions when it needs a GPU.

> [!IMPORTANT]
> GPU MCP is experimental. It helps trusted researchers avoid mistakes, but it
> is not a security sandbox or a cluster scheduler.

## Why does it work well?

- **The human stays in charge.** GPU work is limited by a short permission file,
  and approval applies only to the exact version the human reviewed.
- **The AI gets GPU functions, not an open remote shell.** The GPU machine runs
  only an allowed Python file through a small checker copied into the research
  project.
- **Jobs do not silently collide.** A job reserves its GPU and keeps that
  reservation alive with a heartbeat.
- **Silence is not mistaken for an idle GPU.** If a heartbeat stops, GPU MCP
  checks whether the original program is still running before freeing the GPU.
- **Long jobs are not forgotten.** Each job has an ID, a saved outcome, and a
  planned check time. The shared Codex backend checks quietly and sends useful
  events to the conversation that launched the job.
- **Conversations keep their own jobs.** Codex sessions in one repository share
  a backend while each sees and manages only its own jobs. A separate human
  terminal monitor can show the repository's jobs.
- **Stopping is deliberate.** GPU MCP first shows the process owner, command,
  GPU, and start time. It stops the process only on a second call with the same
  fingerprint, after confirming that nothing changed.
- **Expensive work can start small.** The AI can run a short smoke test before
  the main experiment and keep the link between the two jobs.

## What can the AI do?

| Function | What it does |
| --- | --- |
| `cluster_info` | Gives a quick summary of the GPU machines. |
| `check_gpus` | Shows which GPUs are busy, their memory use, and GPU MCP reservations. |
| `check_gpu_processes` | Shows the programs currently using the GPUs. |
| `run_python_on_gpu` | Reserves one GPU and starts an allowed Python file. It returns a job ID. |
| `build_python_artifact` | Builds a reusable artifact on a configured host, using CPU only or a reserved GPU. Optional policy capability. |
| `manage_gpu_job` | Checks, retries, stops, or finishes a job using its job ID. |
| `list_gpu_jobs` | Lists this Codex conversation's running and completed jobs, including CPU builds. |
| `list_gpu_reservations` | Shows which GPUs have been claimed by GPU MCP jobs. |
| `kill_gpu_process` | Inspects one process, returns its fingerprint, and stops it only after a matching second call. |
| `preview_policy_reload` | Shows exactly what a proposed permission change would do. |
| `reload_policy` | Applies the exact permission change a human approved. |
| `reject_policy_reload` | Discards a proposed permission change. |

For example, you can ask:

```text
Find a free GPU, run jobs/train_model.py, follow the job until it finishes,
and tell me where the output was written.
```

Compilation and execution can be separate. Local CPU builds can use normal
local tools. For remote CPU or GPU-assisted builds, the optional
[`build_python_artifact` tool](docs/builds.md) allows compiler subprocesses inside
a confined build environment. A later ordinary GPU job loads the resulting
library or package; it need not have been built through MCP.

For independent CUDA runs sharing one GPU, [managed MPS sweeps](docs/mps.md)
add a bounded queue and separate case logs, status, stop and explicit retry.
One failed run leaves healthy siblings running. Retries remain the agent's
choice; scientific scripts retain the subprocess restriction.

## How does the human stay in control?

Each research project has a small file named `gpu-mcp.toml`. It lists:

- the GPU machines this project may use;
- the folders containing Python files that may be run;
- the folders those files may change; and
- the folders where logs may be written.

GPU work stays locked until a human reviews this file. GPU MCP then checks it
before every operation, checks the Python file, watches where it writes, and
confirms a program's identity before stopping it. If the permission file
changes, GPU work pauses until the new version is shown and approved.

In the [shared Codex integration](docs/codex-monitoring.md), backend code performs
routine checks and saves completion before releasing the GPU. The hook retains
policy approval checks. Direct stdio clients retain the earlier hook reminders
and agent-requested status checks.

Existing Codex projects can use the [registration upgrader](docs/codex-monitoring.md#migration-and-upgrades)
to enable shared monitoring without recreating their GPU policy or its approval.
After the upgrade, keep using ordinary `codex` and `codex resume`.

This reduces mistakes; it does not make unknown Python safe. A launched file
still has the normal access of the user running it.

## Will it work in my lab?

The current version needs Linux, NVIDIA GPUs with `nvidia-smi`, direct SSH, and
Python 3.11 or newer. The research project must appear at the same path on the
computer running the AI and on the GPU machines. GPU MCP does not currently
submit jobs through SLURM, PBS, LSF, Kubernetes, or cloud GPU services.

## How do I start?

1. Install GPU MCP once on the computer where the AI runs. A private folder in
   your home directory, such as `~/gpu-mcp`, is recommended; the GPU machines do
   not need to see this folder. Follow [INSTALL.md](INSTALL.md).
2. Run the SSH setup yourself and choose the GPU machines to make available.
3. Open your research project with the AI and point it to
   [AI_native_installer/INSTALL_FOR_AI.md](AI_native_installer/INSTALL_FOR_AI.md).
4. Review the proposed `gpu-mcp.toml` and approve it only if the machines and
   folders are correct.

Only the research project needs to live on the shared filesystem. Some labs
also keep one GPU MCP copy for editing and another copy in the home directory
that the AI uses. If yours does, record those paths and the copy command in
`LOCAL_DEPLOYMENT.md`. Git ignores that file so private machine details stay
local.

For deeper detail, see [test/README.md](test/README.md), the [design
decisions](ADR/), and the [official Codex MCP
documentation](https://developers.openai.com/codex/mcp/).

The project is version `0.0.0` and has no open-source license yet.
