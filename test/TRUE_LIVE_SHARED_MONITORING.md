# True Live Shared Monitoring Experiment

## Purpose

Observe two human-opened Codex sessions in one repository. The agents know
normal tool semantics but do not know the job bodies, failure schedule or expected
outcomes. The human does not tell them to poll, stay silent, or announce failures.
The staged contrast round uses two successful smoke checks, then A as the main
failure case and B as the successful main case. Each program publishes simulated
work counts for the human progress bars, without real GPU computation.

This complements the automated integration suite. It measures natural agent
behavior and actual screen traffic; it does not replace deterministic lease,
identity, persistence or policy checks.

## Preparation

Use the repository's approved host list. The agents choose available GPUs through
MCP at launch, including handling a claim race normally. The scripts perform no
GPU calculation, but they still reserve the selected GPUs so real one-minute
lease updates can be observed. CPU-only builds cannot test that heartbeat.

Generate a fresh shared-filesystem fixture repository without starting jobs:

```bash
python test/manual_shared_monitoring.py prepare \
  --repo /shared/path/new-manual-repo \
  --runtime /absolute/verified/gpu-mcp-runtime \
  --python /absolute/path/to/server-and-job-python \
  --hosts APPROVED_HOST_A APPROVED_HOST_B
```

The preparation command creates neutral job names, separate logs, bounded hidden
delays, a repo-local shared MCP registration, human-only prompts/start commands,
and a permission profile denying reads to `jobs`, `.human-test`, `logs` and
`.gpu_mcp_state`. It never starts a model session, approves policy or launches a job.

The same absolute fixture path must exist on both execution hosts. Review and
approve its policy through the normal preview/reload boundary. Use the current
verified runtime; an older home copy would test the older behavior instead.

The local fixture prepared on 2026-10-08 is
`test_mcp_repos/true-live-shared-20261008`. It uses the installed home runtime and
existing approved host list, with no assigned GPU indices. Supplied scripts and
human plan/recording files are denied to model commands. The native sandbox
preflight confirmed those denials and preserved access to ordinary work input.
Public `work/task_a.json` and `work/task_b.json` describe parameters, stage roles,
smoke prerequisites and output paths. They contain no timing or expected failure.
The actual short smoke and contrasting main outcomes remain hidden in the scripts
and human plan. These files are fixture inputs, not a new workflow engine.

Open two new terminals in that directory and use normal `codex`; its local
configuration requests GPT-5.5 medium. For recording, the optional
`.human-test/start-a.sh` and `start-b.sh` helpers run the same native command under
`script`, without a GPU MCP frontend wrapper.

## Blindness Check

The agents must not read the scripts, generated plan, preparation code, observer
trace or either session's terminal recording. Do not use names such as
`expected_failure_after_180_seconds.py` that reveal the hidden schedule.

Before collecting evidence, verify that both sessions actually use the
`manual-blind` permission profile. Beta permission profiles can be overridden
by legacy `sandbox_mode` settings or `--sandbox`; do not assume the generated
profile won simply because it exists. Use `/permissions` or `/debug-config` to
check the effective policy. A harmless, separately prepared denied-read sentinel
can validate enforcement without revealing a job body. See
[Codex permission-profile semantics](https://learn.chatgpt.com/docs/permissions).

If either agent reads a hidden job/plan or learns its expected result/timing,
discard that run and prepare a new fixture. An instruction to avoid reading is
helpful but is not proof of filesystem enforcement.

## Human Procedure

1. Open two terminals in the generated repository and run the corresponding
   `.human-test/start-a.sh` and `start-b.sh` commands. They run ordinary `codex`
   and capture output with `script`. Use GPT-5.5 medium. GPU MCP needs no frontend
   wrapper; normal `codex resume` also works.
2. Complete policy approval first if its exact hash is not already approved.
   The October 8 contrast fixture was explicitly approved by the human after
   expanding its host list; its next sessions need no additional policy review.
   Start a fresh conversation after setup if the
   approval interaction exposed implementation details.
3. Paste only `prompt-a.txt` into session A and `prompt-b.txt` into session B.
   These prompts point to the public task input and let the agents choose resources.
   They do not reveal future outcomes or instruct reporting/polling behavior.
4. Each smoke result enables its owner to validate `smoke.json` and launch the
   linked main job using its own successful smoke job ID. Both jobs use the
   parameters from the public input. A naturally becomes idle after main launch.
   B has ordinary independent CSV analysis
   work. Record whether B is still active at completion; do not claim active-turn
   coverage if that work completed earlier.
5. In a third human terminal, run the read-only observer:

   ```bash
   python test/manual_shared_monitoring.py observe --repo /shared/path/new-manual-repo
   ```

6. While both jobs are running, press Ctrl+G, select the repo monitor and inspect
   both jobs. Escape returns to Codex. Use terminals opened after the one-time
   installer setup. Do not issue manual MCP status calls to help the agents.
   Left/Right switches pages; `v` shows active GPU reservations. Inspect title,
   code, wrapped arguments and simulated bars. `/` searches saved history; try
   `smoke`, `main`, a seed or a task tag. Reservation renewal and completion-file
   checks are available in Details. They are distinct from process inspection.
7. Wait for both main jobs to finish. Keep the initial recording and observer trace;
   inspect the hidden expected plan only when comparing the completed run.

## What to Observe

| Observation | Acceptance evidence |
| --- | --- |
| Shared backend | Both job records have one server instance; native originating thread IDs differ. |
| Smoke continuation | Each successful smoke wakes only its owner; the main launch uses that smoke job ID and matching output parameters. |
| Simulated progress | Smoke bars reach their reported totals; A main stops partway, B main reaches its total. Counts never certify lifecycle or science. |
| Healthy interval | Multiple heartbeat timestamps advance without successful-heartbeat tool rows or routine model wakeups. |
| First failure | Only its originating conversation receives the failure; the other job remains running without a foreign reminder. |
| B succeeds | Only B receives its success event. Record whether useful continuation occurs and whether it adds unnecessary announcements. |
| Output use | B reads and summarizes its final summary/samples; A has partial samples/checkpoint and no final summary. |
| Delivery time | Compare durable outcome time, event acceptance and actual visible alert time. Acceptance alone is not agent action. |
| Repeats | One actionable alert per failure; record any repeated status calls or announcements. |
| Resources | Both outcomes retain correct job/attempt identity and their reservations release only after the required process proof. |
| Human view | Native keyboard entry shows both jobs, preserves the draft, starts no model turn, consumes no notices and closes without affecting jobs. |

The observer reads the human snapshot, reservation timestamps and daemon thread
summaries. It does not poll lifecycle tools, parse outcomes on behalf of a model,
change checking times or consume notifications.

## Reusing the Approved Fixture

The first October 8 round had two failing jobs. A subsequent contrast fixture
changed B to success. The current round adds a short smoke phase before each
main run, seeded parameters, provisional/final artifacts and explicit simulated
progress. Main A retains its 145-second failure and B its 308-second success;
smoke A runs about 35 seconds and smoke B about 55 seconds. Timers begin at each
script's start; GPU selection, tool execution and event delivery add their own
time. The permission profile, Codex registration and approved policy are retained.
The hidden plan marks
the new round's preparation time; the observer ignores older jobs with the same
script names. Previous result and delivery records are retained as history.
Each preparation creates new result directories, preserving previous outputs.
Smoke readiness and parameter matching are verified by the main program as well
as the existing MCP smoke-evidence gate. The fixture requests descriptive titles
and a final output summary but does not prescribe polling, silence, failure
reporting or retries. Record what the agents actually do.

The GPU host list now covers the sixteen real cluster hosts already present in
existing approvals. There is no GPU-model or index restriction; agents discover
current availability. Expanding hosts does change the policy hash. The human's
explicit instruction authorizes recording that exact new hash through the normal
approval store, without weakening the general policy/reload boundary.

## Results and Artifacts

Record completed runs in a local `*_RESULTS.md` report, which Git ignores.
Expected artifacts are `.human-test/plan.json`, `observations.jsonl`, both ANSI
recordings, protected job/event records and actual attempt outcomes. Store no
backend access credentials in published evidence.
