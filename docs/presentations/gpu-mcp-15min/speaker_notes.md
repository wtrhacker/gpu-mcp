# GPU MCP for Polymer Simulators — speaker notes

## 1. Let Codex operate the GPU workflow

The ambition is larger than replacing `nvidia-smi`, SSH, and manual job
submission. We want to give Codex safe hands on our machines so it can stay
with a polymer-simulation problem: inspect the code and prior artifacts, form a
hypothesis, run a bounded experiment, analyze what happened, revise, and try
again.

GPU MCP supplies the machine-facing actions. It does not automate scientific
judgment, but it lets the agent act on that judgment without an unrestricted
remote shell.

## 2. MCP in one sentence

MCP stands for Model Context Protocol. The shortest mental model is “a standard
tool connection for an AI.” Codex remains the planner. An MCP server advertises
named tools with typed inputs and structured outputs.

When I ask for a GPU experiment, Codex can choose `check_gpus` rather than
inventing an SSH command. The MCP server still validates the host, GPU index,
script path, and policy. MCP is the connection to action; a goal, the model's
reasoning, and our scientific criteria are separate pieces.

## 3. The workflow friction we are removing

The manual loop is familiar: inspect several machines, choose a GPU from a
snapshot that may already be stale, launch in the background, remember the PID
and log path, and repeatedly check whether the run ended.

The first benefit of GPU MCP is that a launch returns a stable job ID and status
reports the lifecycle. The deeper benefit is that Codex can use that reliable
loop as part of an investigation: inspect, run, learn, change the setup, and run
again until it reaches a defined criterion or proves a genuine blocker.

## 4. What happens after you ask Codex

The MCP server is a local process next to Codex on the control or login host. It
is not a cloud service.

Each research repo has a `gpu-mcp.toml` policy naming allowed GPU hosts and the
script, output, and write roots. The server uses a dedicated SSH key to reach a
GPU host and stages a guarded Python runner through the shared repo. Managed
state records reservations, heartbeats, process identity, polling cadence, and
outcomes.

Codex's ordinary repo tools let it inspect and edit simulation or analysis
code. GPU MCP is the narrower machine-facing layer that lets those changes be
tested safely on the lab GPUs.

## 5. The MCP is an ensemble of tools

No single tool is the MCP. The MCP is the coherent interface formed by all ten
tools.

Discovery tools expose the fleet and claimable GPUs. Run tools reserve a GPU,
launch work, and manage its lifecycle. Recovery tools expose processes and
reservations when reality is ambiguous. Policy tools let a human inspect and
approve changes to the guardrails.

Together, they are the agent's hands: observe, claim, run, verify, intervene,
and govern.

## 6. The everyday run loop: three tools

`check_gpus` combines live GPU samples with the reservation registry and
distinguishes available, busy, reserved, and unknown GPUs. Unknown fails closed
rather than being treated as free.

`run_python_on_gpu` validates an approved script and paths, atomically reserves
one GPU, and launches a managed background job. It returns a stable `job_id`,
the reservation, an output pointer, and the suggested status cadence.

`manage_gpu_job` follows that `job_id`. Its actions include status, stop, retry,
finish, and cadence updates. The result of one managed run becomes evidence for
the next scientific or implementation decision.

## 7. Heartbeat = ownership lease, not process liveness

The heartbeat is per managed job and is written only by the MCP server instance
that created the reservation. It means “I still own and manage this lease.” It
does not prove that the remote Python process is alive.

Other sessions read the shared reservation but never adopt its heartbeat. If a
heartbeat becomes stale, that is a reason to inspect the recorded remote
process identity—not a reason to free the GPU. A stale reservation remains
reserved when the process is alive or inspection is unavailable. Cleanup is
allowed only after both stale ownership and process-gone proof.

The cadence has an attention role too: `next_poll_after` tells the agent when a
full status check becomes useful. Lease safety protects the GPU; polling
cadence protects the agent's context and attention.

## 8. Hooks are Codex's attention layer

The companion hook is an ordered dispatcher attached to three Codex events.

The policy guard runs across PreToolUse, PostToolUse, and Stop. A stale or
symlinked policy blocks normal work. PreToolUse can then surface an early local
outcome, a due status check, missing smoke evidence before a main launch, or an
unjustified early status check. PostToolUse only rechecks policy drift; job
reminders stay silent there.

At turn end, Stop waits locally until either `outcome.json` appears or
`next_poll_after` arrives. It then continues the same open turn and directs
Codex to call `manage_gpu_job(status)`. Waiting consumes no model turns. It does
not wake a Codex session that has been closed.

The hook only notices readiness. It never SSHes, parses outcomes, writes a
heartbeat, or releases the GPU. Status remains the lifecycle authority. A hook
failure therefore leaves the job and reservation unchanged.

Our personal Codex configuration also has a separate UserPromptSubmit/Stop Git
commit hook. That workflow is not part of GPU MCP and is intentionally absent
from the slides.

## 9. The supporting tools: see, recover, govern

`cluster_info` is the quick fleet dashboard: reachability, GPU counts, average
GPU utilization, and system load. `check_gpu_processes` maps compute processes
to host, GPU, PID, user, command, and memory.

`list_gpu_reservations` exposes active claims for the current user or trusted
group. `kill_gpu_process` is deliberately not broad cleanup: the first call
inspects one owned PID and returns a fingerprint; the second call signals it
only if the identity still matches.

`preview_policy_reload` validates an edited policy, shows the safety-relevant
diff, and returns a one-time token. `reload_policy` activates that exact
approved hash; `reject_policy_reload` discards the token and changes nothing.

## 10. The real goal: sustained scientific investigation

This is the experience we actually want. Give Codex a durable objective with a
verifiable stopping condition: for example, explain why a bead–spring melt is
not meeting an equilibration criterion, run bounded experiments, revise the
setup, and stop only when the criterion is met or a real blocker is evidenced.

Codex can inspect code and artifacts, form a hypothesis, edit the simulation or
analysis, smoke-test the change, launch a managed GPU experiment, wait for the
hook, call status, analyze the evidence, and decide what to try next.

`/goal` makes the objective durable across turns. The heartbeat protects each
long run. The Stop hook reconnects an open interactive turn to the next useful
job event. These mechanisms complement each other; none of them decides whether
the polymer physics is credible.

Already-closed or atomically published intermediate artifacts may be inspected
read-only and reported as provisional. Final-result claims still wait for
terminal status.

## 11. Bounded autonomy: the safety model

The safety model is layered. Repo policy allowlists hosts and paths. A static
source scan and a separate Python runtime audit hook block subprocesses,
sockets, destructive filesystem actions, and writes outside approved roots.
The server atomically reserves a GPU, and every run becomes a managed job with
a heartbeat and outcome record.

This is a cooperative guardrail for trusted lab workflows—not a hostile-code
sandbox. It is also not Slurm and not a validator of force fields,
equilibration, or scientific conclusions.

The point of the guardrails is not to shrink the investigation to one command.
It is to make repeated, prolonged action safe enough to be useful.

## 12. Three things to remember

First, GPU MCP gives Codex hands, not merely visibility. The server retains the
authority behind those actions.

Second, this is a loop rather than a launch command. A goal, the tool ensemble,
heartbeat ownership, and attention hooks keep the investigation moving through
many experiments and decisions.

Third, autonomy must terminate against evidence. We define the scientific
question, credible criteria, allowed action surface, and verifiable stopping
condition.

## Optional live-demo prompt

> Use `gpu-cluster-mcp` to inspect GPU availability. Do not launch anything.
> Explain which tools, heartbeat state, and hook events would support a durable
> investigate–run–analyze–revise loop for one polymer-simulation question.

This demonstrates the architecture without consuming a GPU.

Presenter reference: [OpenAI's “Follow a goal” documentation](https://learn.chatgpt.com/use-cases/follow-goals).
