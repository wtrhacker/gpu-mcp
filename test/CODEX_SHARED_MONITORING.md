# Shared Codex Monitoring Tests

## Purpose

Verify session ownership, automatic backend sharing and quiet monitoring without
weakening the existing policy, lease or build boundaries. This suite is written
from the ADR's behaviors rather than importing the earlier exploratory probe.
Earlier probe reports remain local historical evidence. They are not test
fixtures or required inputs to this suite.

## Coverage

| Test file | What it establishes |
| --- | --- |
| `test_gpu_mcp_codex_state.py` | Native identity, immutable ownership, GPU/CPU result-event transactions, injected event-write failure, restart persistence, attempt conflicts and per-job locks. |
| `test_gpu_mcp_codex_shared.py` | Real server tools with controlled process facts: foreign reads/mutations, listings, smoke/PID boundaries, fixed stale timing, quiet checks, result-before-release, CPU builds, concurrency, policy drift and scoped alerts. |
| `test_gpu_mcp_restart_recovery.py` | Fresh server imports with old completed GPU jobs: direct/shared status, automatic owner recovery, cached results, missing/replaced bookings, fresh/alive/uncertain reservations, transaction failures, locked identity races and a new booking's heartbeat after recovery. |
| `test_gpu_mcp_remote_outcomes.py` | Independent producer/control filesystem views, bounded execution-host reads, result identity checks, unavailable replies, regular-file constraints, retries after alert grace and current process diagnostics. |
| `test_gpu_mcp_codex_delivery.py` | Queued delivery shape, archived/unloaded threads, acknowledgement reconciliation, private success/visible failure instructions and display adaptation. |
| `test_gpu_mcp_codex_transport.py` | Actual localhost backends and stdio gateways, automatic reuse, separate repo identity, native request metadata, agent/human authentication, read-only snapshots and PTY monitor closure. |
| `test_gpu_mcp_codex_concurrency.py` | A slow synchronous tool or human snapshot cannot block health/other calls; native caller context and tool schemas survive HTTP offloading. |
| `test_gpu_mcp_codex_uncertainty.py` | Actual process probe and GPU launch metadata, delayed result visibility/readability, completion before the next scheduled poll, prior missing booking flags and conservative display. |
| `test_gpu_mcp_codex_polling.py` | Real launch metadata, short owner polling packets, idle coalescing, inspection freedom, transient grace/recovery, separate uncertainty episodes and other jobs continuing during retry. |
| `test_gpu_mcp_codex_upgrade.py` | Legacy registration preview/apply/idempotence, exact policy and settings preservation, active GPU/CPU jobs, completion-proof matching and changes during planning. |
| `test_gpu_mcp_codex_versions.py` | Reachable-daemon checks, differing CLI/daemon versions without lifecycle mutations, and refusal of the retired global pin helper. |
| `test_gpu_mcp_codex_cli.py` | Exact local command interception, unchanged ordinary/slash/pasted input, terminal restoration and child survival without monitor data reaching Codex stdin. |
| `test_gpu_mcp_codex_editor.py` | Native helper draft preservation, agent-call refusal, original editor delegation, retained chooser selection and idempotent terminal setup. |
| `test_gpu_mcp_codex_tmux.py` | Existing panes without editor variables, standalone/npm foreground detection, tmux and Byobu configuration, custom-key preservation, real popup paging/logs and unchanged underlying input. An opt-in real Codex TUI check preserves an unsent draft. |
| `test_gpu_mcp_codex_monitor.py` | Left/Right pages, retained job identity, scrolling, history-search editing/Unicode/cancel, standalone arguments, explicit roles, display-only log progress and reservation filtering. |
| `test_gpu_mcp_monitor_display.py` | Runtime versus submission time, title fallback, arguments, reservation/role facts, metadata search, numeric bars with phase labels and malformed progress rejection. |
| `test_codex_shared_monitoring.py` | Opt-in GPT-5.5-medium calls, real CPU fixture builds, native conversation ownership, fork denial, private model context, native terminal captures, visible failure alerts and active-turn queuing. |
| `test_codex_private_polling.py` | Native hidden polling data, owner recall, foreign exclusion, no polling-created user turn and a neutral build task without routine status loops. |

Local tests use pytest-owned policies, registries, worker state and authority
databases. They never launch real GPU jobs or connect to SSH. The live suite uses
the existing daemon and disposable threads; it does not restart the daemon,
change global Codex configuration, or edit the real repository policy.

## Running the Tests

The completed-orphan regression uses temporary reservations and real server
re-imports. It launches no models, GPU computation or SSH work:

```bash
pytest -q test/test_gpu_mcp_restart_recovery.py
pytest -q test/test_gpu_mcp_remote_outcomes.py
```

It covers both legacy JSON persistence and the shared backend's protected
result/event transaction. Recovery must preserve the launching conversation
and old server ID, without adopting a heartbeat or stop/retry authority.

Run local contracts with the runtime Python:

```bash
python -m pytest -q test/test_gpu_mcp_codex_state.py \
  test/test_gpu_mcp_codex_shared.py test/test_gpu_mcp_codex_delivery.py \
  test/test_gpu_mcp_codex_transport.py
```

Check a legacy upgrade and shared HTTP responsiveness without models or GPUs:

```bash
python -m pytest -q test/test_gpu_mcp_codex_upgrade.py \
  test/test_gpu_mcp_codex_concurrency.py test/test_gpu_mcp_codex_transport.py
```

The concurrency cases run a real MCP service through ASGI. A deliberately slow
tool and slow human snapshot each overlap a health request. The tool case also
checks a second native caller and preserved metadata. These checks test HTTP
responsiveness, not remote GPU execution. Thread-to-event-loop wakeups use local
sockets; a restricted outer sandbox can block them just as it blocks localhost
transport. Use the same assertions with the required permission.

Run live Codex acceptance explicitly:

```bash
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 python -m pytest -q test/test_codex_shared_monitoring.py
```

Run just the private-polling contracts and native behavior cases:

```bash
python -m pytest -q test/test_gpu_mcp_codex_polling.py test/test_gpu_mcp_codex_delivery.py
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 python -m pytest -q test/test_codex_private_polling.py
```

The neutral build prompt requests an artifact and its value, with a chosen
checking interval. It contains no no-poll/no-commentary instruction. Useful
result inspection remains allowed. The terminal case separately checks passive
delivery and private receipt lookup; it is not a natural-behavior test.

The live suite fixes both model and reasoning effort to **GPT-5.5 medium** and
checks the daemon's effective settings. No newer model is substituted. It needs
an authenticated, running daemon, a trusted source checkout for native terminal
attachment, bubblewrap for CPU builds and a usable PTY. Its generated policies,
artifacts, structured turn evidence and ANSI captures remain in the pytest
temporary directory printed on failure. Test threads are archived afterward.

Structured evidence and native captures are also retained under ignored
`test_mcp_repos/codex-shared-*`. The latest pointer is
`test_mcp_repos/codex-shared-latest.json`; it includes no backend credentials.
CLI/daemon version equality is not a prerequisite. The existing native daemon
must accept the required initialization/RPCs; the harness never repairs global
feature settings to obtain a passing run.

Socket/bubblewrap acceptance needs an environment that permits localhost socket
creation and Linux sandbox namespaces. A restricted outer Codex sandbox can
block these operations; rerun the same tests with the required permission rather
than skipping the assertions or weakening the worker restrictions.

## Evidence and Limits

The repeated Ctrl+G failure came from an existing tmux shell whose child Codex
frontends inherited no editor setting. Earlier tests sourced the generated
environment in a fresh shell, so they did not cover that activation gap. The
tmux tests start the frontend first with `VISUAL` and `EDITOR` absent, install
the shortcut afterward, then press Ctrl+G through a real attached tmux client.
They inspect private fixture logs, close with Escape and prove the popup's input
and data did not reach the underlying frontend. Source paths containing spaces
and apostrophes exercise the actual tmux/shell quoting.

```bash
python -m pytest -q test/test_gpu_mcp_codex_tmux.py
GPU_MCP_RUN_NATIVE_TMUX_TEST=1 python -m pytest -q test/test_gpu_mcp_codex_tmux.py -k native
```

The native case uses GPT-5.5 medium, submits no prompt and starts Codex with
`--no-daemon` in a disposable tmux server. It does not change the user's daemon,
sessions or GPU jobs. The normal cases use named fixture executables and an
actual Node child process; they are process/terminal contracts, not model tests.

The human contrast run exposed identity/late-result faults that prior fixtures
masked. `test_gpu_mcp_codex_uncertainty.py` reproduces those faults through the
real launch/supervisor path without GPU computation, a model or SSH. Its delayed
file visibility is simulated; it does not measure actual NFS latency.

The October 9 basic-call regression was missed by the earlier sequential
transport checks: a slow SSH GPU scan blocked another gateway's three-second
health check. The new concurrency cases failed before offloading was added.
Two fresh GPT-5.5-medium clients then made five successful native tool calls
through the installed test-repo registration. No jobs were launched. Upgrade
validation also exercised ordinary resume on a disposable conversation and
normal process-proof cleanup of a completed legacy reservation. Saved tool
approvals can block a noninteractive probe that would request approval in an
interactive session.

Keep individual run reports and captured artifacts local. The full source suite
remains the regression gate; the coverage map above identifies the reusable
checks for the earlier ADR invariants.

Native captures distinguish hidden payloads from normal MCP tool rows and model
messages. Private context still consumes tokens and an idle wake can show working
activity. The selected human entry uses the existing native editor keyboard
action. It is checked separately from backend delivery and the wrapper prototype.
The local PTY case also sends actual Left/Right escape sequences through Jobs,
Details, Logs and Events, returns to Logs and closes with Escape. Saved jobs and
events must remain unchanged. Controlled-screen tests separately check selection
after a snapshot reorders jobs and independent scroll positions.
Search includes past jobs and treats shortcut letters as query text. Escape
cancels editing without closing the viewer, and progress reads cannot modify
saved records. A sparse 10 MiB log verifies a maximum 32,000-byte tail read;
non-regular logs are rejected without blocking. The PTY case also types and clears
a search before browsing pages. None of these tests launches a model or GPU job.

Run only the native keyboard acceptance and its local setup contracts:

```bash
python -m pytest -q test/test_gpu_mcp_codex_editor.py
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 python -m pytest -q test/test_codex_shared_monitoring.py -k native_editor
```

This check uses synthetic human-only records and disposable GPT-5.5-medium
threads. It checks retained draft bytes, no monitor-created model turn, selection,
Escape and the native action during a local sleep command. It launches no GPU
jobs and changes no real shell/editor settings.

The wrapper's native check is a separate selected case so it can run without
repeating the four backend/delivery acceptance cases:

```bash
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 python -m pytest -q test/test_codex_shared_monitoring.py -k monitor_frontend
```

The [human-run experiment](TRUE_LIVE_SHARED_MONITORING.md) prepares neutral,
hidden jobs and read-only observation. Its actual interactive sessions are opened
by the human, not by pytest.

The contained live suite uses CPU builds. Real GPU/SSH acceptance remains an
explicit resource test, not a default-suite side effect. Earlier real build/load
results establish the underlying execution path; they do not by themselves prove
the new shared transport on every deployment or Claude compatibility.
