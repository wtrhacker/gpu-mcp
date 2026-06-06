# ADR 0004r: Phase 7 Agentic Polling Tests

## Status

Proposed amendment.

This document amends the Phase 7 test plan for ADR 0004p and
`progress-004.md`. It covers the tests whose purpose is to observe autonomous
agent behavior around GPU job polling. It does not replace deterministic
contract tests for cadence selection, status responses, hook filtering, or
reservation safety.

## 1. Testing Purpose

Phase 7 is motivated by a user-visible agent behavior problem: agents tend to
poll long-running GPU jobs too often and waste context. The MCP should give the
agent a concrete cadence so it can work independently, avoid output-dependent
work, and check the job only when due or when it has an explicit reason to
override.

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

Layer 2: Codex battlefield trace tests.

These run `codex exec` against a simulated current-repo MCP backend and the real
installed GPU MCP companion hook when available. The backend and hook write a
JSONL trace of model-visible events. The trace is the primary artifact. The
scenarios in this ADR are the minimum polling-discipline battlefield set, not
the complete Phase 7 acceptance suite. Smoke validation, cadence-selection edge
cases, update-cadence state handling, and hook-state mechanics should stay in
deterministic pytest unless a specific agent-behavior question needs Codex.

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

## 4. Machine Checks

Battlefield tests must machine-check every deterministic fact before invoking a
judge. Examples:

- the agent did not use raw SSH or shell commands for GPU job state;
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

Scenario A: early poll is compact and the agent stops.

The prompt asks Codex to launch a long GPU job through MCP, continue independent
repo work, and only check the job when it is due. The harness gives a future
`next_poll_after`. If Codex tries status early, the server returns
`not_due_yet`. The expected behavior is that Codex stops polling that job and
does independent work or reports that the job is not due.

Scenario B: user-requested early check uses an override.

The prompt asks Codex to launch a long job and then says the user explicitly
wants an immediate check before `next_poll_after`. The expected behavior is that
Codex calls `manage_gpu_job(action="status", job_id=...,
early_poll_reason=...)` and receives full status.

Scenario C: due reminder triggers status.

The prompt asks Codex to do independent work after launch. The harness advances
fake time past `next_poll_after` before an ordinary next tool call. The
PreToolUse hook emits a due reminder. The expected behavior is that Codex calls
status before doing output-dependent work.

Scenario D: terminal-before-due smoke result is not hidden.

The prompt asks Codex to run a smoke job and then launch a main job based on the
smoke result. The harness records a local terminal smoke outcome before
`next_poll_after`. Machine checks assert that status returns terminal full
status, not `not_due_yet`, and that the main launch includes the smoke `job_id`
when the scenario expects the successful smoke to be used. The judge evaluates
whether Codex uses the smoke result responsibly and does not overclaim that the
smoke runtime is cadence-representative unless the trace supports that judgment.

Scenario E: cross-repo and broad-tool silence.

Repo A has an early or due job. Repo B makes ordinary tool calls, `check_gpus`,
and `list_gpu_reservations`. The expected behavior is no Repo A early-poll or
due reminder in Repo B, and no warning for broad GPU tools that are not
job-specific status calls.

## 8. Runtime Policy

Battlefield tests must use fake time and simulated jobs. They must not sleep for
real heartbeat intervals and must not require live GPUs. The only intentionally
slow part is Codex execution and optional judge evaluation.

Recommended artifacts per run:

- `trace.jsonl`;
- `codex_final.txt`;
- `machine_checks.json`;
- `judge_input.txt`;
- `judge_result.json`;
- pytest summary pointing to artifact paths.

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
