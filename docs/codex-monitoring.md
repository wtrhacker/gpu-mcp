# Shared Codex jobs and monitoring

Several Codex conversations can use GPU MCP in one repository. They connect to
one backend, and each conversation sees and manages only the jobs it launched.
The backend checks jobs quietly. Completion reaches the originating conversation;
failures produce a concise alert there. See [ADR 0008](../ADR/0008-shared-codex-server-and-quiet-monitoring.md).

## Setup and use

Use the Codex launcher in the repository's MCP registration:

```toml
[mcp_servers.gpu-cluster-mcp]
command = "/absolute/path/to/server-python"
args = [
  "/absolute/gpu-mcp-runtime/gpu_mcp_codex_launcher.py",
  "--config",
  "/absolute/research-repo/gpu-mcp.toml",
]
cwd = "/absolute/research-repo"
enabled = true
startup_timeout_sec = 30
tool_timeout_sec = 360
```

Keep the tool approval entries in the [installation guide](../AI_native_installer/INSTALL_FOR_AI.md).
The launcher starts the backend automatically or reuses the existing instance.
Concurrent startups share a lock. Connection processes forward requests; they
do not own jobs. No manual HTTP startup or open monitor is required.

The control host needs MCP SDK v1, Uvicorn, `websocket-client`, and a running
Codex app-server daemon. GPU MCP connects to the existing daemon; it does not
restart it. Native Codex request metadata supplies the conversation identity.
Missing or inconsistent identity is refused. A fork or a new conversation gets
separate access; resuming the original conversation retains its jobs.

The launcher binds localhost and keeps separate private credentials for MCP and
the human viewer. Persistent control state lives under
`~/gpu-mcp/state/codex/<host-and-repo-key>/`, outside approved job write and log
roots. Repository `job.json` files are display mirrors, not ownership authority.
The global GPU reservation registry still coordinates different repositories.

This isolation covers GPU MCP operations and delivered context. Ordinary shared
workspace files and direct access to the local Codex daemon have their existing
filesystem permissions; this is not operating-system isolation between sessions.

## Quiet checks and explicit inspection

The backend checks outcomes once a second using local file information, and
performs process checks when the job's saved checking time arrives. Defaults
are five minutes for smoke jobs, one hour for main/one-off jobs, and thirty
seconds for builds. A changed outcome file can trigger an earlier check.
The one-second scan is not a delivery deadline. On a shared filesystem, a
remote result may become visible later; process proof and queued delivery add
their own time. The human experiment recorded acceptance seven and twenty-five
seconds after the actual exits.

Heartbeat writes remain a separate internal operation: roughly once a minute,
the backend updates the reservation's `last_heartbeat_at` timestamp. Ten minutes
without a successful update permits investigation. Alive or uncertain processes
remain reserved. The agent cannot change this ten-minute threshold. CPU-only
builds have no GPU lease or heartbeat.
In shared mode, check-in errors belong to the affected reservation. Another
job's successful write cannot clear that error or alert a different owner.

Normal scheduled checks save a short private polling update with its check time.
An active owner receives it as hidden context. While idle or unloaded, only the
latest pending update for each job is kept; normal updates do not wake an agent.
Completion and actionable problems can wake it. Manual
tool calls still appear in Codex; their default results are concise. An early
status request explicitly says that no new check occurred. “Running” means the
process was observed running at the recorded check time, not that the science
is correct or progressing.

The registered tool descriptions explain this once. Waiting for a final result
alone needs no sleep-and-check loop. Status, logs and intermediate outputs remain
available for a concrete question or decision. Routine healthy updates require
no human-facing commentary. Completion requires no acknowledgement tool call or
immediate follow-up work; the user's task determines what happens next.
Compact replies label the timestamp `next_backend_check_at`. The existing
`next_poll_after` remains in saved state and full diagnostics.

Temporary verification is retried after two seconds during a ten-second alert
grace. Persistent identity failures retry every thirty seconds; late result
reads remain independent of that deadline. The monitor shows uncertainty while
recovery is pending. A persistent problem then produces one owner alert; a
verified job failure keeps its normal notification. No timeout frees a GPU.

For a finished SSH job, a missing local result also triggers a bounded read on
the execution host. The result must match the saved job, attempt, host, GPU,
reservation and PID. This handles a delayed control-side filesystem view without
rerunning successful work. Invalid or unavailable results remain uncertain;
SSH verification continues with bounded retries. Reconciliation clears old
uncertainty text and updates the process observation used by log diagnostics.
Launch tools accept an optional, single-line `job_title` of at most 120
characters, such as “Learning-rate sweep, seed 3”. This labels the human view;
it does not alter the command, polling, permissions or scientific outcome.
Old records and calls without a title use their script filename. Script paths
and arguments already come from saved launch metadata.

| Request | Result |
| --- | --- |
| `list_gpu_jobs()` | This conversation's running and completed jobs, including CPU builds. |
| `manage_gpu_job(job_id=...)` | Concise status and output pointer. |
| `manage_gpu_job(job_id=..., detail="full")` | Saved job and process/lease diagnostics; an early response remains a local read. |
| `manage_gpu_job(job_id=..., action="logs")` | Bounded log tail; `log_tail_lines` accepts 1–200. |
| `manage_gpu_job(job_id=..., early_poll_reason="...")` | Immediate process inspection when there is a reason to check early. |
| `manage_gpu_job(job_id=..., action="notifications")` | This job's saved notification delivery state. |
| `manage_gpu_job(job_id=..., action="watch", notify_after_sec=..., reason="...")` | A private time-based milestone reminder; it does not estimate scientific progress. |

Stop, retry, finish and checking-time updates keep their existing process proof
and policy gates. Foreign job IDs, reservation keys, smoke references and PIDs
grant no inspection. `list_gpu_reservations(scope="all")` still returns only the
calling conversation's jobs. `check_gpus` reports physical availability without
foreign job details. In this integration, use an owned job's detailed status
instead of the repo-wide `check_gpu_processes` tool.

## Useful events and connection failures

Before ending checks or releasing a GPU, the backend commits the result and its
owner notification together. CPU builds use the same persistence rule. Unknown
outcomes are saved as uncertainty. Backend checking, user actions and delivery
are serialized per job and active attempt.

Polling updates use private history injection and do not start a new user turn.
They are combined while idle and retired when the job's state changes.
Completion and problem events use Codex's queued tool-output interface.
Routine success is private context. Failure and monitoring/lease problems request a concise human
alert. Repeated infrastructure errors are throttled to one alert per job per
ten minutes. The daemon can still show Working/Worked-for activity, and the
model can write commentary; private payloads do not guarantee an entirely
invisible model turn.

Acceptance means Codex accepted the event, not that the agent performed its next
scientific step. A lost reply triggers a saved-history receipt check. Supported
history RPCs are preferred; the current legacy daemon can require a bounded
read of its own local rollout. An unresolved acknowledgement is marked uncertain
instead of automatically creating another turn. Inspect it through the monitor
or the originating job's `notifications` action. An explicit owner
`retry_notification` request requires its `notification_id` and a reason; the
previous message may already have arrived.

Private developer items are omitted from the UI-oriented turn list. Polling
receipts therefore use the bounded daemon-owned history read even when turn
pagination is supported. A lost acknowledgement is not blindly resent.

Archived conversations stay archived. Their events are retained, and delivery
can resume after the human unarchives the conversation. Events are never sent
to another session. A backend restart preserves results and events but does not
adopt another server instance's GPU leases. It saves verified GPU-job completions and
recovers their orphaned reservations once stale, with process-gone proof and a
locked identity/heartbeat check. Alive or uncertain processes stay reserved.
Completion clears the saved check time; recovery updates the booking state and
resolves any pending restart alert for that job. Direct stdio status calls use
the same safe stale cleanup and persist terminal state.

## Human terminal monitor

GPU MCP works through ordinary `codex` and `codex resume`. The selected entry
uses Codex's native Ctrl+G action. The installer configures its helper once:

```bash
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_codex_terminal_setup.py
```

New terminal sessions load this automatically. Users do not need to export
editor variables or change the Codex launch command. The current Codex process
retains its old environment, so first activation needs a new terminal session.
It does not require a daemon restart.

An existing shell can load the installed setting without opening a new terminal.
Quit Codex, run the following once, then use `codex resume`:

```bash
. ~/.config/gpu-mcp/codex-terminal-env.sh
codex resume
```

Fish uses `source ~/.config/gpu-mcp/codex-terminal-env.fish`. Restarting Codex
alone does not reload its parent shell's environment. The installer writes the
setting; it cannot change an already-running shell or frontend's environment.

Press Ctrl+G, select the repository's monitor and press Enter. The existing
viewer uses Left/Right to move through Jobs, Details, Logs and Events without
closing it. The selected job and each page's scroll position are retained.
Escape returns directly to Codex; `b` returns to the repository/editor chooser.
The chooser also offers the user's original text editor. Other applications
delegate to that editor, and repeated setup does not capture the helper as its
own fallback.

This uses Codex's supported terminal handoff. The helper leaves the protected
draft file unchanged when viewing jobs, and terminal output is not returned as
draft text or model input. Repo-wide viewer data stays on the human side. An
ordinary agent shell or PTY call is refused for protected Codex drafts. This
guard does not replace operating-system permissions or the earlier scope limits.

A fresh chooser uses the nearest policy and offers other live repositories when
the terminal's working directory differs from the frontend's current repository.
Select the displayed repo explicitly in that case.

The earlier frontend wrapper remains an optional tested prototype:

```bash
/absolute/gpu-mcp-runtime/bin/gpu-mcp-codex
```

To resume a conversation, append `resume`. Normal Codex arguments pass through,
including `--cd`, model settings and remote daemon connections. Type `/monitor`
in an empty composer and press Enter. The wrapper opens the existing viewer;
Escape returns to Codex. The command and viewer data never enter the model's
input. The CLI and daemon remain the real Codex processes.

This is a terminal adapter, not a stock slash-command plugin. Keep ordinary
`codex` available; an optional shell alias can select the wrapper after installation.
Pasted `/monitor` text and other slash commands pass through normally. Uncertain
editing sequences are handled conservatively; cancel/clear a draft before typing
the local command. Opening the view does not interrupt a running model turn.

Open the viewer directly in a human terminal:

```bash
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_codex_launcher.py \
  --config /absolute/research-repo/gpu-mcp.toml --monitor
```

Alternatively, run `gpu_mcp_codex_monitor.py` from the research repository; it
finds the nearest `gpu-mcp.toml`. The viewer shows repo-wide jobs, their originating
conversation, CPU/GPU resources, status, elapsed time, last check and recent
events. Rows lead with the task title and show code paths, shell-quoted arguments,
job IDs and reservation state. The header counts recorded active GPU reservations
across all conversations in this repository; `v` toggles a view of just those
reservations. CPU jobs and released GPU reservations are excluded from that filter.

Press `/` inside the viewer to search saved jobs by title, script, arguments,
host/GPU, role, status, session or job ID. Matching ignores case and requires
every query word. Completed jobs remain searchable. Enter applies the query;
Escape cancels editing and restores the previous page/selection; Ctrl+U clears
the query. Search is local to the human viewer and sends no model input.

Up/Down selects jobs in the list and scrolls text in the other pages. `[` and `]`
select the previous/next job while inspecting text; Home/End and Page Up/Down
scroll long text. Enter switches between Jobs and Details, `l` opens Logs, and
`r` refreshes. Logs are read on demand and refreshed with `r`. The Events page
shows the selected job's delivery history.

The overview shows saved status, code, separate wrapped argument lines and GPU
reservation state. Normal monitoring/delivery messages and empty scan/progress
placeholders are omitted. Long arguments continue for up to three lines in the
list and remain fully readable in Details. Abnormal reservation/server conditions
still appear in the overview.

Details separates the last reservation renewal, completion-file check and actual
process check. A reservation renewal just maintains the GPU booking; a completion
file check just looks for a published result. Neither proves scientific progress
or a healthy process. These observations are human-only memory, so a backend
restart cannot recover their prior timestamps. Missing observations are omitted.
Script runtime still requires a recorded execution start, not submission time.

## Job roles and reported progress

Smoke, Main, One-off and Build are explicit tags from launch metadata. A Smoke
tag does not mean a job passed validation. Each job has its own progress; a main
job's `smoke_job_id` records its prerequisite separately. Job completion and
successful smoke evidence retain their existing process/result checks.

The viewer draws a bar when the job reports completed and total work. Existing
saved progress is supported. An ordinary Python job can also publish a single
JSON line to its existing stdout log:

```python
print('GPU_MCP_PROGRESS ' + json.dumps({
    'current': completed_steps, 'total': total_steps, 'message': 'sampling',
}), flush=True)
```

Import `json` in the job. Counts are nonnegative integers with a positive total;
booleans and malformed records are ignored. The label is optional. The viewer
reads a bounded log tail through its human endpoint, normally every five seconds
for nearby running jobs, and once on selecting completed history or detecting
its terminal transition. Refresh rereads it. Full logs remain in Logs.

Reported progress is display data: it does not change lifecycle, release a GPU,
certify correctness, or wake an agent. Elapsed time and expected duration are not
converted into work percentages. Uninstrumented jobs, including the supplied
A/B sleep fixtures, show their roles and statuses without a fabricated bar.

Reading the viewer does not poll jobs, change their checking times, or consume
notifications. Closing it leaves the backend and jobs running. Terminal control
bytes in logs are displayed as data. There is no noninteractive JSON-output mode
for feeding this repo-wide view into an agent tool.

A small Python display adapter ports progress formatting, timing, colors and
bounded messages from Scientific Workflow, with its MIT attribution preserved.
Its scheduler and cancellation controls are not imported. See the
[display attribution](../third_party/scientific-workflow/NOTICE.md).
The newer 0.16.1 dashboard was reviewed at commit
[`c6e788e`](https://github.com/dingyisun0101/Scientific-Workflow/tree/c6e788eb4a8e2e8c52cd1a4260b7949fe8a95e2d).
Its separate task labels, allocation summary, retained paging and unknown-value
display inform this viewer. Sweep grouping and resource sampling are possible
later additions; they require real metadata and measurements, not guesses from
arguments. Workflow's receipt/recording migration does not affect GPU MCP.

## Further design opportunities

The display adapter currently takes only task labels, count-based bars, status
colors, bounded messages, allocation summaries and paging ideas. The useful
deeper pattern is separating captured task intent, execution facts and verified
completion evidence. Workflow keeps immutable configuration/dependency snapshots
and checksummed completion receipts; it leaves scientific interpretation to the
consumer. See [its input/receipt contract](https://github.com/dingyisun0101/Scientific-Workflow/blob/c6e788eb4a8e2e8c52cd1a4260b7949fe8a95e2d/protocol/task-inputs-v1.md).

The next small extension could capture an owner-scoped task group, purpose,
prerequisites, expected outputs and acceptance criteria alongside existing title
and arguments. That would support human sweep grouping and the agent asking for
a bounded summary of its own failed or completed tasks. Linked smoke/build/main
jobs could be displayed as a chain without adding a new scheduler.

Subsequent extensions could add measured resource usage and explicit result
summaries, then deterministic subscriptions to declared milestones or measured
failure conditions. Group events could carry changed jobs and artifact/metric
pointers once, with logs fetched on demand. Aggregation must retain owner
isolation, durable acknowledgements, archive handling and uncertainty; it cannot
collapse other sessions' work into the agent's context.

Reuse/retry requires its own contract. Capture relevant inputs and verified
outputs; do not treat matching filenames, a full progress bar or exit zero as
scientific acceptance. Any retry budget and failure policy must be explicitly
declared and preserve existing process proof, reservation and policy gates.
These extensions remain proposals rather than new automatic execution behavior.

An exact custom `/monitor` command remains outside the selected integration.
Native MCP forms and hook entry are a possible later plugin path, but typed
commands enter the turn pipeline. The keyboard handoff opens the human view
before prompt submission. A skill that returns repo-wide viewer output to the
model would violate the separate human-view boundary.

## Migration and upgrades

GPU MCP leaves Codex's shared daemon installation and update channel alone.
Check availability and versions without changes:

```bash
python /absolute/gpu-mcp-runtime/gpu_mcp_codex_versions.py
```

If an older deployment pinned the daemon using `--from-cli`, restore production
updates once through Codex's native maintenance command:

```bash
codex app-server daemon update
```

This maintenance operation can restart the daemon. Normal GPU MCP startup never
performs it. The former `--pin-current-cli` helper now refuses without mutation.
Ordinary CLI upgrades do not require GPU MCP version alignment: native
initialization and actual RPC responses determine compatibility, rather than
exact equality of version strings. A missing daemon or unsupported request
produces an actionable connection failure and preserves pending notifications.

Prefer one canonical CLI installation. Coexisting npm and standalone copies can
make different shells use different versions; updating one does not update the
other. This is installation maintenance, not a GPU-job control responsibility.

A CLI/daemon mismatch can appear as incompatible shared feature settings.
Do not change feature flags to mask that mismatch. Those flags are separate
from ChatGPT subscription versus API-key authentication.

Direct `gpu_mcp_server.py --config ...` remains available for existing stdio
clients. It retains the earlier per-instance ownership and reminder behavior.
Existing jobs without native Codex ownership are not silently assigned to a new
conversation. Drain them through their original server before switching that
repository to the launcher.

For an existing Codex project, the installed upgrade helper replaces the direct
entrypoint with its sibling shared launcher. Preview the change, then apply it:

```bash
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_codex_upgrade.py \
  --config /absolute/research-repo/gpu-mcp.toml
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_codex_upgrade.py \
  --config /absolute/research-repo/gpu-mcp.toml --apply
```

The installer runs this upgrade for already-configured repositories. It verifies
the existing policy approval, changes only the server-script argument, saves a
backup and preserves other servers, timeouts, environment and tool approvals.
The policy file and approval history are not rewritten. Repeating it after a
successful upgrade returns `already_shared` without edits.

`blocked` lists unfinished legacy jobs. The check includes GPU bookings, local
job records and protected CPU-build records; a build completion marker alone
does not establish completion. Finish those jobs through their original server
before applying the upgrade, and pause new launches during the change. Completed
old bookings remain until normal availability checks prove the original process
is gone and reread unchanged metadata under the shared lock.

Quit and resume the same conversation after applying the upgrade. This refreshes
its MCP command and tool catalog while retaining conversation history. An
already-running legacy MCP process cannot change executable in place. The helper
does not issue a global MCP reload, restart the daemon or adopt old jobs. Run the
terminal setup helper once if the keyboard monitor was not installed; new
terminals then load it automatically.

Keep the global policy hook. A shared launcher registration disables its
recurring repo-wide job reminders and Stop wait; policy drift and approval
checks remain active. This behavior is Codex-only. `claude-version/` is unchanged.

A live backend with different code refuses reconnection instead of being replaced
over active jobs. After draining it, the human can stop the idle backend:

```bash
/absolute/path/to/server-python \
  /absolute/gpu-mcp-runtime/gpu_mcp_codex_launcher.py \
  --config /absolute/research-repo/gpu-mcp.toml --stop-if-idle
```

The next configured session starts the updated backend automatically. The command
refuses while jobs or requests are active. Deploy changes to the installed runtime
before reconnecting; normal startup does not restart the Codex daemon.

[Test commands and acceptance evidence](../test/CODEX_SHARED_MONITORING.md)
describe the tested boundaries and remaining native UI/remote-resource limits.
