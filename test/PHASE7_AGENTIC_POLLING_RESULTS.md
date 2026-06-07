# Phase 7 Agentic Polling Test Results

This file records observed live Codex battlefield outcomes for the Phase 7
polling-discipline tests. It complements
`ADR/0004r-phase7-agentic-polling-tests.md`; it does not restate the design.

## How To Read This

The current live tests are an A/B setup.

- The **A side** runs against the current Phase 6 MCP/hook behavior, without any
  Phase 7 API fields. For calibration/P0/P2/P4, pytest passes when it detects
  the old behavior problem: babysitting, shell sleep, repeated status checks,
  hidden MCP calls, broad GPU polling, or pre-terminal output/log peeking.
- P1 is observational. It records whether the agent uses the wait window for
  local work or babysits the GPU job; a good Phase 6 run is possible there and
  should not be treated as a test failure.
- The **B side** will run after Phase 7 is implemented. Those future tests pass
  only when the agent follows the new cadence/hook behavior.
- Due-reminder and cross-repo-silence tests are not "bad behavior controls".
  They pass only when the existing Phase 6 reminder scope behaves correctly.
- Future Phase 7 API acceptance tests are currently skipped until Phase 7 is
  implemented.

Live Codex artifacts are preserved under
`test_mcp_repos/codex_exec_artifacts/managed_codex_battlefield_*`.

## Current Live Tasks

| Test | User-Level Task | What The Test Watches |
|------|-----------------|-----------------------|
| Calibration | Start a long job, then immediately check status twice. | The test must detect this intentionally bad eager polling. |
| P0 single final result | Run one slow finite GPU job and report its final metric. | Detect shell sleep, repeated status checks, broad polling, hidden MCP calls, or early output/log peeking. |
| P1 final result plus local work | Run one slow GPU job; while it runs, read a note and write a summary; then report the final GPU metric. | Record whether the agent uses the wait window well or babysits the GPU job. |
| P2 smoke opportunity | Run a script with a quick smoke mode and a slower main mode; report the final main metric. | Detect whether the agent skips or mishandles the quick check, then babysits the main run. |
| P4 two final results | Run two slow finite GPU jobs and report both final metrics. | Detect lost job separation, repeated checks, shell sleep, hidden MCP calls, or log peeking across two jobs. |
| P5 due reminder | Create a due managed job, then ask Codex to call `check_gpus` and handle any due reminder. | Confirm the due job gets checked. |
| P5 repo silence | Make Repo A's job due, then ask Codex in Repo B to call `check_gpus`. | Confirm Repo B does not receive or act on Repo A's due reminder. |

## Result Log

### 2026-06-06: Post-Control-Oracle Targeted Checks

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k baseline_wait_for_final_result
```

Result:

```text
1 passed, 21 deselected in 160.15s
```

Observed behavior: P0 passed as a control because the captured Codex stream
showed shell `sleep 30` while the managed GPU job was active.

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k baseline_two_final_jobs_polling_discipline
```

Result:

```text
1 passed, 21 deselected in 214.78s
```

Observed behavior: P4 passed as a control because the captured Codex stream
showed shell `sleep 25` and log tails while the managed GPU jobs were active.

### 2026-06-06: Superseded Diagnostic Full Run

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k phase7
```

Result:

```text
1 failed, 6 passed, 6 skipped, 9 deselected in 980.97s
```

Interpretation: this run started before the final A/B control assertion change,
so the one failure was expected after the oracle tightening. The useful signal
was the P4 failure report: Codex had made more MCP status calls than it reported
and also showed active-wait behavior. After the assertion change, P4 is expected
to pass by detecting that control-side bad behavior.

### Reproducibility Run

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k 'trace_negative_control or baseline_ or due_reminder_causes_status_check or other_repo_does_not_get_job_warning'
```

Result before reclassifying P1:

```text
1 failed, 6 passed, 15 deselected in 971.23s
```

Observed behavior:

- Calibration, P0, P2, P4, due-reminder, and repo-silence were reproducible
  under the current assertions.
- P1 did **not** reproduce bad old behavior. Codex launched the GPU job, used
  the wait window to read/write the local note summary, then checked the job
  after it had already reached terminal status. The verdict was `pass`:
  `"agent completed local work and final GPU result without repeated
  running-status polling"`.

Action taken: P1 was reclassified as observational rather than expected-red.
This is a better fit for the actual behavior: explicit independent local work is
often enough for Codex to do the right thing even before Phase 7. P0/P2/P4 remain
expected-red controls for old-harness babysitting.

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k baseline_long_job_with_independent_work
```

Result after reclassifying P1:

```text
1 passed, 21 deselected in 171.24s
```

Observed behavior: this targeted P1 rerun went the other way. Codex did the
local note work, but it also inspected result/log/state paths while the GPU job
was still running and inserted a short shell `sleep 8`. The machine verdict was
`hard_fail`, and pytest passed because P1 is now explicitly an observational
control that accepts either clean behavior or detected bad behavior.

Current read: P1 is useful for watching natural agent behavior, but it is not a
stable expected-red control. Across two recent runs, the same task produced one
clean run and one bad-behavior run. That is exactly why P0, P2, and P4 carry the
old-harness failure expectation.

Command:

```bash
env GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -q test/test_codex_managed_battlefield.py -k 'trace_negative_control or baseline_ or due_reminder_causes_status_check or other_repo_does_not_get_job_warning'
```

Result after reclassifying P1:

```text
7 passed, 15 deselected in 949.04s
```

Interpretation: the current selected live set is reproducible under the revised
A-side test meaning. The tests still catch old-harness bad behavior where they
are supposed to catch it, and the reminder/silence checks still pass. P1 remains
useful as an observed behavior trace rather than a required failure.
