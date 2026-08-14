# GPU MCP for Polymer Simulators — speaker notes

## 1. Let Codex operate the GPU workflow

Most of us already use Codex to write or inspect simulation code. The question
today is: how do we let it help with the operational part of a GPU run without
giving it an unrestricted shell? Our answer is a small MCP server specialized
for our lab workflow.

The goal is not to automate scientific judgment. It is to spend less attention
on SSH, `nvidia-smi`, PIDs, log redirection, and cleanup—and to get a clearer
record of what actually happened.

## 2. MCP in one sentence

MCP stands for Model Context Protocol. The shortest mental model is “a standard
tool connection for an AI.” Codex remains the planner. An MCP server advertises
named tools with typed inputs and structured outputs.

When I ask for a GPU smoke test, Codex can choose `check_gpus` rather than
inventing an SSH command. The MCP server still validates the host, GPU index,
script path, and policy. MCP does not move authority into the model; it gives
the model a narrow interface to authority we control.

## 3. The workflow friction we are removing

The manual loop is familiar: inspect several machines, choose a GPU from a
snapshot that may already be stale, SSH, launch in the background, remember the
PID and log path, then decide whether “the process disappeared” means success or
failure.

With GPU MCP, a launch returns a job ID immediately, and status uses that ID to
report the lifecycle and output location. The payoff is not simply fewer
commands; it is less ambiguity about the run.

## 4. What happens after you ask Codex

The MCP server is a local process next to Codex on the control or login host. It
is not a cloud service.

Each research repo has a `gpu-mcp.toml` policy. That file names allowed GPU
hosts and the script, output, and write roots. The server uses a dedicated SSH
key to reach a GPU host and stages a guarded Python runner through the shared
repo. Managed state records the reservation, heartbeat, process identity,
polling cadence, and eventual outcome.

The four boundaries are: Codex plans; MCP validates and tracks; SSH transports;
the guarded runner executes one approved Python file.

## 5. The MCP is an ensemble of tools

This is the key idea behind the added slides: no single tool is the MCP. The MCP
is the coherent interface formed by all ten tools.

The discovery tools tell Codex what exists and what is claimable. The run tools
reserve a GPU, launch work, and manage its lifecycle. The recovery tools expose
processes and reservations when reality is ambiguous. The policy tools let a
human inspect and approve a change to the guardrails.

The composition matters: observe, claim, run, verify, intervene if necessary,
and evolve policy explicitly.

## 6. The everyday run loop: three tools

These are the three tools a normal simulation run should use most often.

`check_gpus` samples every configured host and combines live utilization with
the reservation registry. Its result distinguishes available, busy, reserved,
and unknown GPUs. Unknown fails closed rather than being treated as free.

`run_python_on_gpu` validates an approved script and paths, atomically reserves
one GPU, and launches a managed background job. It returns a stable `job_id`,
the reservation, an output pointer, and the suggested status cadence.

`manage_gpu_job` follows that `job_id`. Its actions include status, stop, retry,
finish, and cadence updates. In the common path, Codex launches once and asks
status until it gets a terminal outcome.

## 7. The supporting tools: see, recover, govern

`cluster_info` is the quick fleet dashboard: node reachability, GPU counts,
average GPU utilization, and system load. `check_gpu_processes` goes deeper and
maps compute processes to host, GPU, PID, user, command, and memory.

`list_gpu_reservations` exposes active claims for the current user or the whole
trusted group. `kill_gpu_process` is deliberately not broad cleanup: the first
call inspects one owned PID and returns a fingerprint; the second call signals
it only if the identity still matches.

Policy changes are also tools. `preview_policy_reload` validates the edited
policy, shows the safety-relevant diff, and returns a one-time token.
`reload_policy` activates that exact approved hash; `reject_policy_reload`
discards the token and changes nothing.

These supporting tools are used less frequently, but they are what make the
normal run loop observable, recoverable, and governed.

## 8. A polymer workflow, end to end

Here is the experience we want. I ask for a bounded bead–spring equilibration
smoke test, followed by the production run only if the smoke succeeds.

Codex checks availability, launches the smoke job, and receives a stable job
ID. Status interprets the launcher outcome rather than guessing from
`nvidia-smi`. If the smoke succeeds, the main job gets its own reservation and
heartbeat. Finally, status reports the terminal outcome and output location.

Codex can own that operational plumbing. It does not determine whether our
equilibration criterion, force field, timestep, or ensemble is scientifically
appropriate.

## 9. Bounded autonomy: the safety model

The safety model is layered: repo policy allowlists hosts and paths; jobs are
Python-only and run under a bounded-write guard; the server atomically reserves
a GPU before launch; and every run becomes a managed job with heartbeat and an
outcome record.

Uncertainty fails closed. A stale heartbeat alone never proves that a remote
process is gone, so the GPU stays reserved unless process identity checks give
sufficient evidence.

This is a cooperative guardrail for trusted lab workflows. It is not Slurm,
not a hostile-code sandbox, and not a validator of polymer physics.

## 10. Three things to remember

First, MCP is a bridge: it gives Codex typed tools without giving away the
server's authority.

Second, our MCP is the ensemble. Discovery, launch, lifecycle, diagnosis,
recovery, and policy tools work together rather than behaving like isolated
commands.

Third, we retain the scientific judgment. This system makes the operational
record safer and clearer; it does not validate the physics.

A good first trial is deliberately small: ask Codex to launch one bounded smoke
job, then report the job ID, terminal outcome, and output path.

## Optional live-demo prompt

> Use `gpu-cluster-mcp` to check GPU availability. Do not launch anything yet.
> Explain which tool you called, summarize the structured result, and tell me
> which tools would carry a bounded smoke job from launch to terminal outcome.

This prompt demonstrates tool discovery and policy-bounded inspection without
consuming a GPU.
