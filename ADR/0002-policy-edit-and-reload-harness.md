# ADR 0002: Policy Edit and Reload Harness

`gpu-mcp.toml` is the repo-local server policy for GPU MCP. It controls which
hosts, script roots, write roots, output roots, and timeouts the MCP server may
use for one research repo.

This ADR defines how that policy may be changed during an interactive Codex
session without weakening the long-horizon safety model.

## Status

Accepted and implemented for the server-side boundary and deterministic hook
helper.

Implemented:

- `gpu_mcp_policy_approval.py` manages approved-policy records and audit
  history under `~/gpu-mcp/state/approved-policies.json` by default.
- `gpu_mcp_doctor.py approve-policy` records human-approved policy hashes.
- `gpu_mcp_server.py` checks policy approval at startup.
- `gpu_mcp_server.py` exposes `preview_policy_reload`, `reload_policy`, and
  `reject_policy_reload`.
- Reload tokens are one-time, in-memory only, bound to the previewed policy
  hash, expired after a short TTL, and capped so abandoned previews cannot
  accumulate forever.
- The active in-memory policy changes only through `reload_policy`.
- Normal MCP cluster tools refuse while `gpu-mcp.toml` on disk is stale
  relative to the active in-memory policy.
- `gpu_mcp_policy_hook.py` provides small PreToolUse and PostToolUse
  policy-drift hook behavior for Codex-style clients. It is installed as a
  user-global GPU MCP companion hook and discovers the relevant repo by walking
  upward from the hook working directory.

Still human-supervised:

- Interactive Codex hook acceptance, because automated tests can verify the
  hook output but cannot prove the human approval judgment.
- Codex hook trust/acceptance, because hook registration and trusted hashes are
  client-version and local-config dependent.

ADR numbering should stay as-is. ADR 003 was implemented first because remote
execution needed to be decoupled from the server install path before full
acceptance testing. ADR numbers record decision history, not execution order.

## Context

The normal GPU MCP safety target is a long-running Codex session where the human
is not approving every small step. In that mode, the agent may run GPU jobs,
debug scripts, inspect GPUs, or kill a confirmed owned process, but it must not
silently broaden `gpu-mcp.toml` or `.codex/config.toml` to make a rejected task
work.

At the same time, humans may reasonably ask the same interactive Codex session
to update repo policy because they do not want to restart, lose context, or hand
edit TOML. That workflow should be supported, but it must be explicit.

There are two separate events:

1. Editing `gpu-mcp.toml`.
2. Making the running MCP server use the edited policy.

The first event is just a file edit. The second event is the security-sensitive
operation. The server must never automatically reload policy merely because the
file changed. The active in-memory policy changes only at startup or after an
explicit preview/reload approval checkpoint.

## Decision

The server security boundary is approval by policy hash.

The MCP server enforces policy only at startup and reload time. It does not
monitor file edits in real time. If an agent edits `gpu-mcp.toml`, the running
server continues using the old active policy. The edit only matters on restart
or explicit reload, where approval and hash checks apply.

Policy mutation and policy activation are separate operations:

- The policy file may be edited by the human or by an agent acting under direct
  human instruction.
- The edit is inactive until the server previews, validates, and reloads it.
- Reload requires a one-time token bound to the exact previewed file hash.
  Tokens are in-memory only; they do not survive server restart, expire if not
  used promptly, and may be evicted if too many previews are abandoned.
- Normal GPU tools should refuse to continue while the policy file on disk is
  stale relative to the active in-memory policy.
- If the human rejects the preview, the agent calls `reject_policy_reload` or
  the implementation's equivalent token-discard path, then stops. A rejected
  token is invalidated and cannot later be used for reload.
- If the human asks to replace a rejected inactive candidate, the hook should
  allow a narrow direct edit of `gpu-mcp.toml` while continuing to block normal
  GPU work. The agent must preview the edited candidate again before reload.

The optional Codex hook is an agent-workflow layer. It catches policy edits
early, interrupts confused agent behavior, and tells the agent what to do next.
It is still not the authority that makes a policy safe. The server-side hash
checks remain the boundary if the hook is missing, bypassed, or not supported by
another MCP client.

## Server Security Model

The server's job is:

> Never use an unapproved policy.

It does that with four checks.

### 1. Startup Approval Check

When the MCP server starts, it reads `gpu-mcp.toml`, computes its SHA256 hash,
and checks the approved-policy record. If the record says this exact policy path
is approved at this exact hash, startup may continue.

If the file changed after approval, the hash is different and the server refuses
to start. Therefore, an agent cannot edit `gpu-mcp.toml`, restart Codex/MCP, and
silently gain the new policy.

### 2. No Automatic Hot Reload

After startup, the server holds the active policy in memory.

If `gpu-mcp.toml` changes on disk, the active in-memory policy does not change.
For example:

```text
active policy hash = abc123
file on disk hash  = def456
```

The server is still enforcing `abc123`. The edited file `def456` is only a
candidate. It is not active policy.

### 3. Preview Reload

If the human wants the edited policy to become active, the agent calls
`preview_policy_reload`.

The server reads the candidate file, validates it, compares it with the active
policy, and returns:

- active policy hash;
- candidate policy hash;
- safety-relevant diff summary;
- validation result;
- one-time reload token if validation passes.

This is not approval yet. It is the server saying: "Here is exactly what would
change."

### 4. Hash-Bound Reload Token

The reload token must be bound to the exact candidate hash returned by preview.

Otherwise an agent could do this:

1. edit policy: add `gpu-b`;
2. call preview, which shows only "added gpu-b";
3. human approves that diff;
4. edit policy again: also add a broad write root;
5. call reload, applying a policy broader than the one the human reviewed.

The hash-bound token blocks this. When `reload_policy(token=...)` is called, the
server recomputes the current file hash. Reload succeeds only if that hash still
matches the previewed candidate hash. If the file changed after preview, reload
is refused and the agent must preview again.

## Stale Policy Refusal

The server should refuse ordinary GPU tools when the policy file on disk no
longer matches the active policy hash.

Allowed while stale:

- `preview_policy_reload`;
- `reload_policy` with a valid token;
- `reject_policy_reload` or the implementation's equivalent token-discard path;
- a narrow direct edit of `gpu-mcp.toml` when the human explicitly asks to
  replace the inactive candidate;
- a future `policy_status`-style diagnostic that only explains the active hash,
  file hash, and next procedural step.

Refused while stale:

- `run_python_on_gpu`;
- `check_gpus`;
- `cluster_info`;
- `check_gpu_processes`;
- `kill_gpu_process`;
- any other normal cluster action.

The line is intentionally conservative: if a tool touches hosts, GPUs,
processes, SSH, or cluster state, it is a normal cluster action and must refuse
while the policy file is stale. Diagnostics allowed in stale state must be
policy-only diagnostics.

The refusal message should be explicit and should not invite the agent to fix
the situation silently:

```text
gpu-mcp.toml has changed but has not been reloaded.
Active policy is still the old approved policy.
Do not revert the file. Do not edit Codex config or unrelated files.
Stop immediately and explain to the human what you were trying to do,
what changed, and why GPU MCP refused to continue.
If the human intentionally changed the policy, the next step is
preview_policy_reload. Show the safety diff and call reload_policy only
after explicit human approval.
Only edit gpu-mcp.toml while stale after explicit human rejection or
cancellation of the prior candidate and explicit human re-orientation to the
next candidate edit.
```

This is secure and clearer than silently continuing under the old policy. It
also handles the common workflow mistake where the human or agent edits
`gpu-mcp.toml` but forgets that the running server has not loaded it.

The "do not revert" instruction is intentional. A policy file changed outside
the expected edit-preview-approve-reload path is evidence that the workflow is
out of bounds. The right response is to stop and surface the state, not to hide
it by restoring files and continuing.

## Reload Flow

The same-session policy maintenance flow is:

1. Human asks for a policy change.
2. Agent edits `gpu-mcp.toml`.
3. Agent calls `preview_policy_reload`.
4. The server validates the file and returns:
   - current active policy hash;
   - candidate file policy hash;
   - safety-relevant diff summary;
   - validation result;
   - one-time reload token if validation passes.
5. Human approves or rejects the diff in chat.
6. If the human rejects it, the agent calls `reject_policy_reload(token=...)`
   or the implementation's equivalent token-discard path, does not reload, and
   stops. The candidate file remains inactive until the human edits, reverts, or
   asks for a new preview. If the human asks to supersede the candidate, the
   hook may allow a narrow direct edit of `gpu-mcp.toml`; the agent then calls
   `preview_policy_reload` again.
7. If the human approves it, the agent calls `reload_policy(token=...)`.
8. Server verifies that the current file hash still matches the previewed hash.
9. Server records the approved hash and audit history.
10. Server reloads the policy into memory.
11. Normal GPU tools are allowed again because the file hash and active hash now
   match.

The checkpoint must remain explicit: edit, preview, human approval, reload.

Reload should be ordered so partial failure is safe. The approval-store write
should happen before the in-memory active policy changes. If the process crashes
before the store write, the old policy remains active and restart rejects the
unapproved candidate. If the process crashes after the store write but before
the in-memory update, the running server still has the old active policy and
normal tools remain stale-refused; a restart or a new preview/reload can recover.
This may be inconvenient, but it is fail-closed with respect to unapproved
policy activation.

## Approved Policy Record

The approved-policy record should live outside the research repo, preferably
under the trusted GPU MCP install directory:

```text
~/gpu-mcp/state/approved-policies.json
```

The installer and Codex config should treat `~/gpu-mcp/` as trusted control
state. Codex should not be allowed to edit this directory during normal repo
work.

Example shape:

```json
{
  "/abs/research/repo/gpu-mcp.toml": {
    "current_hash": "abc123",
    "history": [
      {
        "hash": "abc123",
        "approved_at": "2026-05-26T00:00:00Z",
        "approved_by": "human",
        "summary": {
          "nodes": ["gpu-a", "gpu-b"],
          "script_roots": ["jobs"],
          "write_roots": ["results", "/tmp/gpu_mcp_outputs"],
          "output_roots": [".gpu_mcp_logs"],
          "sync_timeout_sec": 300
        },
        "diff_summary": [
          "added node gpu-b",
          "added write root /tmp/gpu_mcp_outputs"
        ]
      }
    ]
  }
}
```

The history is not meant to reconstruct the full Codex conversation. It is meant
to make policy broadening visible later.

## Config Path And Symlinks

The server should treat the `--config` path as a real repo-local policy file,
not as an alias that can be redirected later.

`gpu-mcp.toml` should not be a symlink. The server should resolve the explicit
absolute `--config` path, reject a symlink at that path, and record the resolved
path in the approval store. This keeps policy identity simple: one repo policy
path maps to one approved hash.

If a symlink target changes, the hash would change and startup/reload checks
would refuse or require a new preview. But relying on that behavior makes the
policy path harder for a human to reason about. Rejecting symlinked policy files
is simpler and matches the rest of the design: script and output boundaries
already avoid symlink tricks instead of treating them as normal workflow.

## Hook Behavior

Hooks are useful agent engineering. They are not the final authority.

A Codex hook should be small, final-purpose, and workflow-oriented:

- run before and after Codex tool use as user-global `PreToolUse` and
  `PostToolUse` companion hooks for GPU MCP;
- find the nearest `gpu-mcp.toml` by walking upward from the hook working
  directory, exiting quietly when the current workspace has no GPU MCP policy;
- compare that file's hash with the approved-policy record;
- if the file is unchanged and approved, exit quietly;
- if the file is changed or unapproved, tell the agent that the edit is
  inactive, normal GPU work must stop, and the next step is
  `preview_policy_reload` only if the human intended the edit;
- allow recovery tools such as `preview_policy_reload`, `reload_policy`, and
  `reject_policy_reload` while stale;
- allow a narrow direct edit of `gpu-mcp.toml` while stale when the human asks
  to replace the inactive candidate;
  This exception must be target-based, not tool-name-only: structured edit
  events may carry `file_path` or `path`, while patch-style update events may
  carry the patch body as `patch`, `cmd`, top-level `command`, or a raw string.
  The hook should allow the edit only when every touched file resolves to the
  discovered `gpu-mcp.toml`;
- tell the agent not to revert the policy file silently and not to keep trying
  cluster actions until the human has reviewed the state;
- include the human-edited versus Codex-edited branch in the hook output,
  because external agents will not read this ADR;
- never mark a policy as approved;
- never reload policy by itself.

This hook improves agent behavior because modern LLM agents usually follow
clear procedural feedback well. It catches confusion at the moment it happens.
But the system remains safe if the hook is absent: the server will not start or
reload with an unapproved hash.

The hook should not require a repo-local mode file, snapshot file, cache, or
environment variable to decide whether policy edits are safe. The approved
policy record is the comparison target. The simpler rule is: policy edits are
allowed only as file edits; activating the edit requires the server's
preview/reload flow. A hook may make this obvious to the agent, but it should
not become a second permission system.

The hook intentionally does not monitor `.codex/config.toml`. A change to
`.codex/config.toml` affects future Codex sessions, not the already-running MCP
server. That belongs to installer/doctor/startup validation. The hook's job is
current-session policy drift for `gpu-mcp.toml`.

The hook is best-effort feedback. Codex hook coverage and Bash interception are
client-side behavior, so the server's stale-policy refusal remains the reliable
backstop. In the tested Codex build, `PostToolUse` blocks/replaces the result of
the just-finished tool call, but the model may still try another tool or write a
final response. Registering the same policy check as `PreToolUse` blocks
subsequent normal tools before they execute while the policy is stale.

When the hook fires after Codex edited `gpu-mcp.toml`, the agent should say:

```text
gpu-mcp.toml has changed but is not active.
Do not revert it. Stop normal GPU work.

If the human edited this file, call preview_policy_reload and show the raw
preview output, including diff_summary and hashes.

If you edited this file, tell the human you changed it, then call
preview_policy_reload and show the raw preview output, including diff_summary
and hashes. Ask the human to inspect gpu-mcp.toml before approving reload.
```

The hook does not need to detect who edited the file. The runtime agent knows
whether it just made the edit in the current conversation. The hook output must
tell the agent what to do with that knowledge. This is not merely documentation:
these instructions must be emitted in the hook result so an arbitrary Codex
agent using the MCP sees them.

`preview_policy_reload` should also return procedural fields or text that say:

```text
Show this raw preview output, including diff_summary and hashes, to the human.
Do not summarize it as the only evidence.
Call reload_policy only after explicit human approval.
If you edited gpu-mcp.toml yourself, remind the human to inspect the file before
approving reload.
```

These reminders belong in runtime hook/tool output, not only in ADRs, README
files, or tests.

Codex config must set `reload_policy` to `approval_mode = "prompt"`, not
`"approve"`. The prompt is visible to the human in the Codex UI; after the human
approves it, the agent only receives the normal MCP result and cannot infer from
that result that approval occurred.

## Policy Edit Paths

There are two valid same-session paths.

Human-authored edit:

1. Human edits `gpu-mcp.toml`.
2. Human tells Codex: "I edited the GPU MCP config; preview and reload it."
3. Codex calls `preview_policy_reload`.
4. Codex shows the raw server output: diff summary, active hash, candidate hash,
   and reload token status.
5. Human says whether to approve reload.
6. If approved, Codex calls `reload_policy(token=...)`.
7. If rejected, Codex calls `reject_policy_reload(token=...)` or the equivalent
   discard path, then stops.

Codex-authored edit:

1. Human asks Codex to edit `gpu-mcp.toml`.
2. Codex edits the file.
3. The PostToolUse hook fires after the tool call and tells Codex normal GPU
   work must stop.
4. Codex explains that the edit is inactive and does not revert it.
5. Codex calls `preview_policy_reload` immediately. Preview is read-only and
   does not activate the candidate policy.
6. Codex shows the raw server output and tells the human to inspect
   `gpu-mcp.toml` before approving reload.
7. Human says whether to approve reload.
8. If approved, Codex calls `reload_policy(token=...)`.
9. If rejected, Codex calls `reject_policy_reload(token=...)` or the equivalent
   discard path, then stops.

The social-engineering boundary is intentionally human-supervised rather than
fully automated. Codex must show the raw server output, not only its prose
summary. If Codex authored the edit, it must also tell the human to inspect the
file before approval. This does not try to defeat a malicious model that lies
about everything; it prevents silent broadening in the expected AI-native
workflow.

## Environment Variables

Environment variables are rejected as production authority.

An environment variable may be convenient for tests or logging, but it must not
grant permission to edit policy, bypass approval, or choose the approved-policy
store in normal operation. An agent can edit future launch configuration or
write alternate files; therefore env vars are too easy to confuse with real
approval.

Rules:

- Normal installs should use fixed trusted paths under `~/gpu-mcp/state/`.
- No production env var should disable policy approval.
- Any remaining test-only env var must be named as test-only, omitted from
  AI-native installer docs, and guarded so it is only honored under pytest.
- The running server may read startup configuration, but startup configuration
  is not human approval.

## Server Responsibilities

The MCP server is responsible for:

- loading `gpu-mcp.toml` from an explicit absolute `--config` path;
- checking the approved-policy record at startup;
- refusing startup if the policy file is unapproved or changed since approval;
- serving normal tools only when the active hash matches the policy file hash;
- previewing candidate policy changes;
- issuing one-time hash-bound reload tokens;
- expiring or evicting abandoned reload tokens;
- invalidating rejected reload tokens without changing active policy;
- refusing reload if the file changed after preview;
- recording the approved hash and audit history before switching the active
  in-memory policy;
- applying the new policy in memory only after successful approval recording.

`_apply_policy` is the implementation step that replaces the server's in-memory
settings after a successful reload: repo root, allowed nodes, script roots, write
roots, output roots, and timeout. It does not edit files and it does not approve
policy by itself.

## Rejected Alternatives

### Automatic Hot Reload

Rejected because it lets a policy edit become active without a checkpoint. That
is exactly the bypass shape the policy system is meant to prevent.

### MCP Tool For Candidate Editing

Rejected after live testing. A special MCP tool for rewriting the inactive
candidate was more complex than the problem: new MCP tools are not available in
already-running Codex sessions, and a fresh MCP server may refuse startup while
`gpu-mcp.toml` is stale. The correct recovery path is hook-level: keep normal
GPU work blocked while stale, but allow a narrowly targeted edit of
`gpu-mcp.toml` itself, followed by a fresh `preview_policy_reload`.

### Hook As Security Boundary

Rejected because a hook is client-side workflow control. It may not be installed
for every client, it may not see every possible edit path, and its behavior may
change with Codex versions. Hooks are still useful as early warning and agent
instruction, but server-side hash approval remains the boundary.

### Environment Variable Mode Switch

Rejected as an authority mechanism. Env vars can be changed in future launch
configuration and are easy for an agent to route around. Approval state belongs
in trusted install state under `~/gpu-mcp/state/`.

### Repo-Local Mode File

Rejected because the agent can usually write the research repo. A repo-local
mode file would let the agent switch itself into a more permissive workflow.

### Trusted Session-Mode State File

Rejected. A separate `session-mode.json` creates a second state machine beside
the approved policy hash: protected, maintenance, stale, approved, and so on.
That extra state is easy to misunderstand and does not provide the real security
boundary. The useful part of the idea is the hook feedback, not a new mode
authority.

### Deterministic External Approval UI

Rejected for this project. A separate approval CLI could display the canonical
diff outside the agent conversation and write a trusted approval nonce. That
would reduce social-engineering risk, but it is too heavy for a rare
human-supervised policy edit workflow. The accepted design instead lets Codex
call the non-activating preview automatically, requires Codex to show raw server
output, and requires the human to inspect the file before approving reload when
Codex authored the edit.

### Restart-Only Policy Changes

Rejected as the only workflow. Restart-only changes are simple, but they do not
match the desired interactive use case where a human asks Codex to adjust policy
without losing context.

## Testing Implications

Deterministic tests should cover:

- approved-policy record stores hash, summary, and history;
- startup rejects unapproved or changed policy;
- preview rejects invalid policy;
- preview reports host/root/timeout diffs;
- reload requires a valid preview token;
- reload refuses expired preview tokens;
- abandoned preview tokens are capped;
- human rejection invalidates the pending token and does not reload policy;
- reload rejects token reuse;
- reload rejects if the policy file changes after preview;
- reload updates active in-memory policy;
- normal tools refuse when the policy file is stale relative to active policy;
- refusal text tells the agent to stop and explain instead of silently
  reverting/editing policy;
- hook helper detects `gpu-mcp.toml` drift against the approved-policy record
  and emits the correct procedural warning;
- hook helper allows policy preview/reload/reject tools while stale;
- hook helper allows narrow stale-time edits of the discovered `gpu-mcp.toml`
  for structured `file_path`/`path` edit events and patch-style update events
  carried as `patch`, `cmd`, top-level `command`, or raw string;
- hook helper refuses mixed patches, unrelated file edits, and add/delete patch
  operations while stale.

`codex exec` battlefield tests should cover the MCP-facing parts of this
lifecycle: startup approval checks, protected policy-mutation failure, preview
output, token rejection, token reuse rejection, stale-policy refusal, and reload
changing the active in-memory policy.

Interactive hook behavior should remain a manual acceptance check, not a normal
automated suite requirement. The important human behavior is:

1. start an interactive Codex session in a repo with an approved policy;
2. ask Codex to edit `gpu-mcp.toml`;
3. confirm the hook warns that the edit is inactive;
4. confirm a subsequent normal tool call is blocked before execution by
   PreToolUse;
5. confirm Codex shows raw `preview_policy_reload` output;
6. inspect `gpu-mcp.toml` because Codex authored the edit;
7. approve or reject the reload in chat;
8. after reload, confirm normal GPU work resumes;
9. confirm later policy edits again cause stale-policy refusal until preview and
   approval.

Automated tests should not pretend to simulate the human approval judgment. They
should test the mechanics around that judgment and document the manual
acceptance recipe for real interactive sessions.

## Consequences

This design preserves fast long-horizon GPU work while allowing intentional
same-session policy maintenance.

It keeps the MCP server honest: the server enforces the active policy, but does
not pretend it can stop the outer Codex client from editing repo files. The hook
improves agent behavior, while the server hash checks provide the actual safety
boundary.

The implementation should remain small because the boundary is narrow, not
because the hook is provisional. This is not a general permission system or a
hostile-code sandbox. It is a deterministic checkpoint for one sensitive
repo-local policy file.
