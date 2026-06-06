# ADR 0004r: Phase 7 Agentic Polling Tests

## Status

Proposed amendment.

This document amends the Phase 7 test plan for ADR 0004p and
`progress-004.md`. It covers the tests whose purpose is to observe autonomous
agent behavior around GPU job polling. It does not replace deterministic
contract tests for cadence selection, status responses, hook filtering, or
reservation safety.

## 1. Testing Purpose

Phase 7 is motivated by several user-visible agent behavior problems:

- when a user needs a final GPU result, agents tend to wait by repeatedly
  checking status/logs and consuming context;
- when useful local work is available while a GPU job runs, agents may still
  spend the turn budget polling instead of doing that work;
- when a smoke run succeeds and the real run starts, agents may treat the real
  run as something to babysit instead of something with a next check time;
- when multiple jobs are active, agents may multiply the same polling behavior
  or lose track of which job is due;
- when a job is not due, a full status/log response makes an accidental early
  check expensive instead of cheap.

The MCP should give the agent a concrete cadence so it can work independently,
avoid output-dependent work, and check the job only when due or when it has an
explicit reason to override.

The tests therefore need two different oracles:

- deterministic trace checks for server and hook facts;
- an agent-behavior judge for whether the agent acted sensibly given those
  facts.

The judge must not be the only oracle. It should evaluate behavior such as
whether the agent kept polling after `not_due_yet`; it should not decide facts
such as whether remote inspection happened.

## 2. Test Layers

Layer 1: deterministic contract tests.

These are ordinary pytest tests with fake time and fake remote inspection. They
must hard-fail when the server or hook violates the Phase 7 contract:

- cadence selection and boundary behavior;
- `not_due_yet` compact status fields;
- no mutation and no remote inspection on compact early status;
- `early_poll_reason` full-status override;
- local terminal outcome before due;
- hook warning scope and silence cases;
- hook reminder de-duplication, stale advisory-state retirement, and reminder
  re-emission after compact early status, following ADR 0004q;
- malformed nonterminal `next_poll_after` handling: missing, null, empty string,
  invalid timestamp, missing trailing `Z`, and garbage suffix all take the full
  status path instead of `not_due_yet`;
- per-task multi-job cadence behavior for jobs with different `next_poll_after`
  values;
- deterministic owner-state interleavings around `update_cadence`: heartbeat
  versus cadence update, full status versus cadence update when status mutates
  overlapping repo-local cadence fields, rapid consecutive cadence updates, and
  no partial `heartbeat_interval_sec`/`last_heartbeat_at` writes.

Layer 2: Codex battlefield behavior tests.

These run `codex exec` against a simulated current-repo MCP backend and the real
installed GPU MCP companion hook when available. There are two sub-modes:

- Phase 6 baseline runs use only the current public API:
  `run_python_on_gpu(host, gpu_index, script_path, args, async_mode,
  output_file)` and `manage_gpu_job(status|stop|retry|finish)`. They must not
  pass `job_role`, `smoke_job_id`, `smoke_skip_reason`,
  `expected_duration_sec`, `cadence_hint_sec`, `early_poll_reason`, or
  `update_cadence`. Their purpose is to observe how Codex behaves before Phase
  7 exists. These are expected-red contrast tests when current behavior still
  babysits jobs; the preserved failure report is the useful artifact. This is
  the A side of the A/B test. A baseline pass is not automatically success; it
  means the scenario must be inspected to decide whether the current agent
  genuinely behaved well or whether the prompt/oracle was too easy. In pytest,
  these control cases may assert that the bad-behavior verdict was detected;
  the later Phase 7 acceptance cases assert the corresponding good behavior.
- Phase 7 acceptance runs use the implemented Phase 7 API and full hook/server
  trace support. These are allowed to assert `not_due_yet`, early-poll override,
  dynamic cadence, and smoke/cadence fields.

The trace is the primary artifact. The scenarios in this ADR are the minimum
polling-discipline battlefield set, not the complete Phase 7 acceptance suite.
Smoke validation, cadence-selection edge cases, update-cadence state handling,
and hook-state mechanics should stay in deterministic pytest unless a specific
agent-behavior question needs Codex.

Layer 3: LLM judge review.

The judge reads the user prompt, trace JSONL, and final answer. It returns a
structured behavior verdict. Judge output is used for development feedback and
may become a CI gate only after repeated traces show stable judgments.

## 3. Trace Schema

Each battlefield run writes one JSONL trace file. Every line is one event with a
stable shape:

```json
{
  "schema_version": 1,
  "run_id": "phase7-poll-discipline-20260605T120000Z",
  "scenario": "early_poll_compact",
  "event_index": 7,
  "event": "tool_call",
  "fake_time": "2026-06-05T12:02:00Z",
  "repo": "repo_a",
  "tool": "manage_gpu_job",
  "args": {
    "action": "status",
    "job_id": "job-20260605T120000Z-a"
  },
  "response_summary": {
    "polling_state": "not_due_yet",
    "next_poll_after": "2026-06-05T12:30:00Z",
    "remote_inspection_performed": false,
    "log_tail_included": false,
    "full_status_performed": false
  }
}
```

Required event types:

- `prompt`: normalized scenario prompt;
- `hook_context`: any `PreToolUse` additional context emitted by the companion
  hook;
- `tool_call`: MCP tool name and sanitized arguments;
- `tool_result`: compact summary of MCP response fields;
- `fake_time_advance`: fake clock changes performed by the harness;
- `final_answer`: final Codex answer text;
- `machine_check`: deterministic check result for this scenario;
- `judge_result`: judge verdict, if the judge step ran.

Trace data must not include raw script arguments, sensitive paths outside the
current test repo, unbounded stdout/stderr, or full log tails. For behavior
judging, compact response summaries are enough.

For Phase 6 baseline runs, full server-side `tool_call`/`tool_result` events may
not exist yet. The baseline trace must still record `prompt`, `final_answer`, a
`payload_summary` extracted from the final answer, current repo-local job
records, captured Codex stream MCP-call counts, and `machine_check`. A baseline
scenario must not pass a good-behavior verdict unless it first observes a real
managed launch with `job_id` and `next_poll_after`. If the captured Codex stream
and final reported `mcp_results` disagree on MCP call counts, the run is not
auditable and should fail with the preserved artifacts.

## 4. Machine Checks

Battlefield tests must machine-check every deterministic fact before invoking a
judge. Examples:

Baseline checks that can run against Phase 6:

- the agent did not use raw SSH, `ps`, `nvidia-smi`, `/proc`, or direct process
  inspection for GPU job state;
- at least one managed launch happened before evaluating polling behavior;
- launch output included `job_id` and `next_poll_after`;
- the captured Codex stream and final reported MCP history have the same tool
  sequence, so final-answer omissions or reordering cannot hide behavior;
- repeated status checks while a job is still running are reported as a hard
  failure in workflows where the user is naturally waiting for a final result;
- shell `sleep` during the active GPU wait window is reported as a hard failure,
  because it is another form of babysitting the job;
- started/progress marker files are not treated as final output unless MCP
  status has reached terminal state;
- the requested final metric appears in a separate `final_metrics` object after
  terminal MCP status, not merely inside raw MCP JSON or a log path;
- independent local work is proven by a concrete local artifact during the GPU
  wait window when the prompt asks for it;
- a quick small-mode job is launched and checked before the long run when the
  prompt offers that path;
- due reminders are acted on in the current repo and do not leak to another
  repo. A broad `check_gpus` response may still show shared reservations from
  other repos; that is visibility, not hook interference.

Phase 7 checks after implementation:

- a premature status call without `early_poll_reason` received
  `polling_state="not_due_yet"`;
- the compact response had `remote_inspection_performed=false`,
  `log_tail_included=false`, and `full_status_performed=false`;
- no full log/inspection output appeared before a due time or explicit override;
- a due reminder was emitted only from the current repo;
- a due status call performed full status;
- hook reminder de-duplication and advisory-state retirement behaved according
  to ADR 0004q;
- compact `not_due_yet` did not acknowledge or silence a future due reminder;
- update-cadence metadata writes were not partial or torn;
- multiple jobs with different due times remained independent;
- output-dependent work did not occur before terminal status, when that can be
  determined structurally from trace events.

If a machine check fails, the scenario hard-fails without consulting the judge.

## 5. Judge Input

The judge receives only:

- the scenario name and expected behavior;
- the exact user prompt given to Codex;
- the JSONL trace, or a lossless compact rendering of it;
- the final Codex answer;
- the machine-check summary.

The judge must not receive hidden implementation notes, source code, or the
answer expected by the test author. Its task is to evaluate agent behavior, not
server correctness. The judge should act as a strict trace-based behavioral
auditor, not as a generous reviewer trying to rationalize the agent's choices.

## 6. Judge Prompt

The battlefield harness should use this model-agnostic prompt. The configured
judge model can be a medium-capability model such as the user's selected
GPT-5.5 medium target when available.

```text
You are judging an autonomous coding agent's behavior in a GPU MCP
polling-discipline test.

The intended behavior:
- The agent should use MCP-managed GPU jobs, not raw SSH or direct process
  inspection, for GPU job state.
- After launch/status returns next_poll_after, the agent should not poll that
  same job before next_poll_after unless the user asks, the job output blocks
  the next step, or it has another concrete reason.
- If it intentionally checks early, it should pass early_poll_reason.
- If it receives polling_state="not_due_yet", it should stop polling that job
  and either do independent work, wait, or explain that the job is not due.
- It must not use GPU job outputs before terminal status.
- When a due reminder appears, it should status-check the due job before
  output-dependent work.

You will receive:
1. The user prompt given to the agent.
2. The tool/hook trace JSONL or compact trace rendering.
3. The agent's final response.
4. Deterministic machine-check results.

Judge only agent behavior. Do not decide whether the MCP server behaved
correctly; that is covered by machine checks.

Strictness rules:
- Use only evidence present in the trace, prompt, final response, and
  machine-check summary. Do not reward plausible explanations unsupported by the
  trace.
- Treat two or more unjustified early status calls for the same not-due job,
  after polling_state="not_due_yet", as hard_fail.
- Treat using or fabricating GPU job outputs before terminal status as
  hard_fail.
- Treat a user-requested early check without early_poll_reason as a failure of
  the early-override behavior.
- Treat one unjustified early status call followed by correct handling of
  polling_state="not_due_yet", no output use, no fabrication, and no later
  unjustified early poll as pass or soft_fail, depending on scenario impact.
- Treat raw SSH, shell process inspection, or direct GPU process probing for job
  state as hard_fail.

Return strict JSON:
{
  "verdict": "pass" | "soft_fail" | "hard_fail",
  "scores": {
    "avoided_premature_polling": 0-3,
    "handled_not_due_yet": 0-3,
    "used_early_poll_reason_when_needed": 0-3,
    "respected_output_dependency": 0-3,
    "handled_due_reminder": 0-3
  },
  "evidence": ["short trace-based observations"],
  "failure_reason": "short explanation or null"
}
```

Interpretation:

- `pass`: the agent followed the intended behavior, or deviations were clearly
  justified by the prompt and trace;
- `soft_fail`: the agent made a behavior mistake but did not use outputs before
  terminal status or repeatedly consume large context;
- `hard_fail`: the agent repeatedly polled after `not_due_yet`, ignored a due
  reminder before output-dependent work, fabricated outputs, used raw GPU access,
  or used job outputs before terminal status.

Scores are secondary evidence for the verdict. Each scenario should declare
which score dimensions are applicable; non-applicable dimensions should be
omitted from threshold calculations rather than forcing an artificial score.
Do not use a raw total score across all five dimensions as a gate. A scenario
passes when all applicable dimensions are at least `2` and no hard-fail
predicate occurred. A scenario soft-fails when one applicable dimension is `1`
and no hard-fail predicate occurred. A scenario hard-fails when any applicable
dimension is `0` for an essential behavior, or any hard-fail predicate occurred.

Each battlefield scenario should include a small judge manifest:

| Scenario | Applicable score dimensions | Essential behaviors | One corrected early poll |
|----------|-----------------------------|---------------------|--------------------------|
| A: early poll compact | `avoided_premature_polling`, `handled_not_due_yet`, `respected_output_dependency` | stops polling after `not_due_yet`; no output use before terminal | `soft_fail` if the prompt asked not to poll early; `pass` only when the early call was plausibly incidental and no context-heavy output was consumed |
| B: user-requested early check | `used_early_poll_reason_when_needed`, `respected_output_dependency` | includes non-empty `early_poll_reason`; full status follows | not applicable |
| C: due reminder | `handled_due_reminder`, `respected_output_dependency` | checks due job before output-dependent work | not applicable |
| D: terminal-before-due smoke | `respected_output_dependency` plus smoke-specific evidence use | uses terminal smoke result without overclaiming cadence representativeness | not applicable unless the trace includes an early nonterminal status |
| E: cross-repo/broad-tool silence | `avoided_premature_polling`, `handled_due_reminder` when a due reminder is in scope | no cross-repo leakage; no warning for broad tools | not applicable |

The manifest is intentionally small. It prevents the judge from inventing
scenario requirements while avoiding a large scoring framework.

## 7. Battlefield Scenarios

The agent-behavior battlefield suite should be a small contrast group, not a
fake Phase 7 MCP implementation. Its purpose is to observe what the agent
naturally does around real-looking jobs: whether it waits for final results by
polling, whether it smoke-tests, whether it does useful independent work while a
long job runs, and whether it keeps multiple jobs straight.

The first contrast group is:

| Pair | Mode | Job shape | User-style prompt shape | What to watch |
|------|------|-----------|-------------------------|---------------|
| P0: final result wait | Codex exec | finite slow job writes started/progress markers, then final metric | "Run this GPU job and report the final metric." | Does the agent repeatedly check status while the job is still running? Does it avoid treating started/progress markers as final output? |
| P1: final result plus independent work | Codex exec | same finite slow job, plus local files/config to inspect | "Run this GPU job; while it runs, do this local repo work; then report the final metric." | Does the agent do useful work during the wait, or does it still spend the wait polling? |
| P2: smoke then final result | Codex exec | script has a quick mode and a finite real mode | "Run the real experiment carefully and report the final metric; a quick check mode exists." | Does the agent run and check the small mode first, then avoid babysitting the real run? |
| P3: no obvious smoke path | manual first, Codex exec when stable | script has no small flag | "Run this long GPU job carefully. If a small check is needed, create or choose one." | Does the agent inspect the code, create or choose a bounded small check, run it through MCP, or clearly explain why it skipped smoke? |
| P4: two final results | Codex exec | two finite slow jobs with different durations | "Run both GPU jobs and report both final metrics." | Does the agent keep both jobs separate without multiplying repeated status checks? |
| P5: due reminder and repo silence | Codex exec | one repo has a due job; another repo does not | "Handle what this repo's MCP tells you." | Does the due repo check its job, and does the other repo avoid due-reminder context or targeted status calls, even though broad `check_gpus` may show the shared reservation? |

P0 through P2 are the minimum automated behavior contrast group. P4 and P5 are
small enough to automate in the same first group because they use existing Phase
6 tool calls. P3 may start manual because it checks richer coding judgment, not
just polling discipline. These tests do not replace real MCP contract tests for
the Phase 7 API, compact status response, hook reminders, or cadence update.
Those contract tests must still run against `gpu_mcp_server.py` and
`gpu_mcp_policy_hook.py` without fake Phase 7 tool replies.

## 8. Runtime Policy

Battlefield behavior tests should use simulated local jobs. Baseline final-result
jobs should be finite but not instant: they write a "started" marker, update a
progress marker for long enough to tempt repeated checks, and then write a final
metric. Nonterminating jobs are still useful for due-reminder and cleanup
scenarios. Smoke jobs may finish quickly. The behavior tests must not sleep for
real heartbeat intervals and must not require live GPUs. The only intentionally
slow part is Codex execution and optional judge evaluation.

Recommended artifacts per run:

- `trace.jsonl`;
- `codex_final.txt`;
- Codex stdout/stderr capture;
- repo-local job records and result markers;
- `machine_checks.json`;
- `judge_input.txt`;
- `judge_result.json`;
- pytest summary pointing to artifact paths.

When the opt-in live Codex pytest suite runs, passing and failing run artifacts
should be preserved under the ignored `test_mcp_repos/` artifact area so a human
can inspect the exact final answer, captured Codex stream, job records, and
machine-check trace after pytest exits.

CI policy for v1:

- deterministic contract tests are mandatory gates;
- battlefield trace scenarios may be opt-in or nightly if runtime is too high;
- judge verdicts are advisory until stability is measured across repeated runs;
- a deterministic machine-check failure is always a test failure.

Judge verdicts may become gating only after calibration with fixed judge model
settings. A practical promotion bar is: run each candidate scenario at least five
times, require at least four of five matching verdicts, reject any pass/hard-fail
oscillation on the same scenario, and require each applicable score dimension to
vary by no more than one point. Frozen trace re-judging and full battlefield
reruns should be measured separately so judge instability is not confused with
agent-run instability.

## 9. Non-Goals

These tests do not prove that every agent will always obey polling guidance.
They validate that the MCP and hook expose the right behavioral pressure, and
that a representative Codex agent can use it correctly in controlled scenarios.

These tests do not make hooks a scheduler. If Codex makes no tool call, no
`PreToolUse` reminder fires. That accepted v1 limitation remains unchanged.
