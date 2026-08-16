# ADR 0005 Interactive Hook Results

## Purpose

Verify in one real interactive Codex session that managed-job events reach the
agent without moving lifecycle ownership out of `status`:

- active work receives an outcome through `PreToolUse`;
- a turn waiting in Stop continues on an outcome or scheduled status time; and
- terminal `status` reports the result and releases the target reservation.

## Environment

Tests ran on 2026-08-07 using the installed user-global hook at
`/home/tingran/gpu-mcp/gpu_mcp_policy_hook.py`, the production GPU MCP server
on ledenberg, real SSH launches on `wiz.mit.edu` GPU 0, and durable state in
the shared Polycomb repository. The accepted failure tests used real managed
launchers and real process exits. No test manually created or edited an
`outcome.json`.

Short delays inside the probe scripts made event ordering deterministic. They
were test synchronization, not production polling behavior.

## Results

| Scenario | Managed job | Observed result |
|---|---|---|
| Success at scheduled cadence | `job-20260807T183937Z-6G1PQsvd` | A 45-second job exited 0. Stop continued at the scheduled status time. The first status encountered a brief shared-filesystem visibility race and retained the reservation; the next status read the outcome, reported success, and released it. |
| Failure before cadence while waiting | `job-20260807T184131Z-tJQECV3c` | The job exited 7 before its five-minute cadence. Stop continued when the local outcome appeared. One status reported failure and released the reservation. |
| Real SIGSEGV before the PreTool fix | `job-20260807T190148Z-b8TK4qDq` | The outcome recorded signal 11 at `19:02:03Z`, before the `19:06:48Z` cadence. Two ordinary tool calls produced no PreTool reminder. Stop then continued the turn; status reported signal 11 and released the reservation. This exposed the missing outcome check in PreTool. |
| Real SIGSEGV after the PreTool fix | `job-20260807T192219Z-ReNSQ4-R` | The outcome recorded signal 11 at `19:22:31Z`, before the `19:27:19Z` cadence. An ordinary filesystem tool call received the PreTool outcome reminder at `19:22:55Z`; Stop was not involved. Status reported signal 11 and removed the `wiz.mit.edu.gpu0` reservation. |

The post-fix reminder state recorded the active attempt exactly once:

```json
{
  "last_outcome_attempt_id": "attempt-20260807T192219Z-8Fwy8PXS",
  "last_outcome_reminded_at": "2026-08-07T19:22:55Z",
  "outcome_reminder_count": 1
}
```

The target job records are terminal with `active_reservation: false`. The
reservation registry contains no reservation for either accepted
`wiz.mit.edu.gpu0` crash test.

## Discarded Probe

`job-20260807T190036Z-z1Lnqb-g` attempted to generate SIGSEGV with
`os.kill`. The remote safety guard rejected that API, so the process exited 1
with no signal. It verifies guard behavior but is not SIGSEGV evidence.

## Artifacts

- `.gpu_mcp_logs/stop_hook_live_test.log`
- `.gpu_mcp_logs/stop_hook_failure_live_test.log`
- `.gpu_mcp_logs/stop_hook_real_sigsegv_live_test.log`
- `.gpu_mcp_logs/adr0005_pretool_sigsegv_live_test.log`
- `.gpu_mcp_state/jobs/<job-id>/job.json`
- `.gpu_mcp_state/jobs/<job-id>/attempts/<attempt-id>/outcome.json`

The disposable probe scripts were removed. Logs and durable managed-job records
were retained as evidence.

## Durable Intermediate Analysis Amendment

On 2026-08-09, the installed hook and server source were updated to distinguish
provisional intermediate analysis from final-result consumption. The live test
used the real 100,000-particle simulation on `kulibin.mit.edu` GPU 0.

While `job-20260810T024732Z-c1hQeSrd` was running and before
`next_poll_after`, Codex read the already-closed `starting_conformation_0.h5`
and computed a structural overlap check. It did not inspect the remote process
or claim a final result. A subsequent premature status call returned compact
`not_due_yet` with no remote inspection. When `outcome.json` later appeared,
PreToolUse prompted status immediately; terminal status reported success and
released the reservation.

The installed hook emitted the amended guidance during this test. The MCP
server process in that existing Codex session predated deployment and retained
its old in-memory guidance string; the synchronized installed source is covered
by the server contract tests and is loaded by new MCP server processes.
