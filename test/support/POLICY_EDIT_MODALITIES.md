# Policy Edit Modalities Tested

This note records the policy-edit and policy-reload behaviors that have actually
been exercised. It is intentionally narrow. It does not summarize the whole GPU
MCP test suite; it only covers `gpu-mcp.toml` edits, stale-policy blocking,
preview/reload, hook behavior, and the remaining human-facing checks.

## The Core State Machine

The active MCP server keeps one policy in memory. Editing `gpu-mcp.toml` on disk
does not automatically change that active policy.

The tested flow is:

```text
approved active policy
  -> gpu-mcp.toml changes on disk
  -> normal GPU tools refuse as stale
  -> preview_policy_reload validates and returns raw preview output:
     diff_summary, hashes, and a token
  -> human approves reload
  -> reload_policy(token) activates exactly the previewed hash
  -> normal GPU tools work again
```

The important design point is that preview is non-activating. It does not change
the active policy or approved-policy record, although it does create temporary
in-memory token state. It is allowed while the policy file is stale and does not
need a separate human checkpoint. The human checkpoint is external Codex
approval before `reload_policy`, because `reload_policy` is where the server
changes the active in-memory policy.

## Modality 1: Deterministic Reload Contracts

Test file:

```text
test/test_gpu_mcp_policy_reload_contract.py
```

This is the main automated coverage for ADR 0002. It exercises the server
without real Codex, SSH, or GPUs.

What it proves:

- an unapproved policy file is rejected at startup;
- an approved policy file can start the server;
- `preview_policy_reload` returns active hash, candidate hash, diff summary,
  raw agent instructions, and a one-time reload token;
- invalid policy changes produce a validation error and no token;
- `reload_policy` refuses invalid, expired, or already-used tokens;
- `reload_policy` refuses if `gpu-mcp.toml` changes after preview, because the
  current file hash no longer matches the previewed candidate hash;
- `reject_policy_reload` discards a token without activating the candidate;
- ordinary tools refuse while the policy file is stale;
- recovery tools remain callable while stale: preview, reload with a valid
  token, and reject;
- preview tokens have a TTL and cap so pending reload state cannot grow forever;
- stale-policy checking avoids rehashing unchanged policy files on every tool
  call.

Recent focused run:

```text
pytest -q test/test_gpu_mcp_policy_reload_contract.py
passed as part of: 21 passed with the hook contract tests
```

## Modality 2: Deterministic Hook Contracts

Test file:

```text
test/test_gpu_mcp_policy_hook_contract.py
```

This tests the helper used by the Codex `PreToolUse` and `PostToolUse` hooks.
It does not try to simulate Codex's UI.

What it proves:

- the hook can find `gpu-mcp.toml` from a subdirectory;
- the search is bounded and does not walk arbitrarily far up the filesystem;
- if the policy hash no longer matches the external approval store, the hook
  returns a blocking JSON object;
- the block message tells the agent not to revert and to use
  `preview_policy_reload`;
- the message now treats preview as part of the recovery flow, not as a separate
  human approval checkpoint;
- the hook allows `preview_policy_reload`, `reload_policy`, and
  `reject_policy_reload` to run while stale so the agent can recover;
- when the policy file is approved, the hook emits no replacement JSON.

Recent focused run:

```text
pytest -q test/test_gpu_mcp_policy_hook_contract.py test/test_gpu_mcp_policy_reload_contract.py
21 passed
```

## Modality 3: Interactive Hook And Reload Walkthrough

This was tested manually in the live interactive Codex session because the
important behavior involves human approval and the live hook surface.

Steps exercised:

1. A harmless comment was added to `gpu-mcp.toml`.
2. The hook fired after the edit and blocked normal GPU work.
3. `preview_policy_reload` was called while stale.
4. The raw preview output showed:
   - `active_hash`;
   - `candidate_hash`;
   - `config_path`;
   - `diff_summary`;
   - `reload_token`;
   - runtime `agent_instructions`.
5. The human explicitly approved reload.
6. `reload_policy(token=...)` succeeded and updated the active hash.
7. `cluster_info` worked after reload, showing normal tools were unblocked.
8. The harmless comment was removed.
9. The cleanup edit went through the same preview and explicitly approved
   reload path, returning the policy to the earlier approved hash.

Observed result:

```text
policy edit -> hook blocks normal work -> preview works while stale
-> human approves -> reload succeeds -> normal tools resume
```

This walkthrough also exposed a workflow issue: originally the hook text made it
sound like the agent should ask before preview. That was too slow and not the
right boundary. The runtime text and ADR were changed so the agent previews
immediately and asks only before `reload_policy`.

### Rejected Candidate Superseding

Another live walkthrough intentionally cancelled the `reload_policy` approval
after a comment-only policy edit had already been previewed. The important
positive observation was that broad normal work stayed blocked while the policy
file was stale against the active server hash. That is the intended safety
posture from ADR 0002's stale-policy refusal section.

The weaker UX was the recovery path after cancellation:

```text
policy edit -> preview succeeds -> human cancels reload
-> policy file remains stale against the active hash
-> normal reads/edits/tools are blocked by the hook
-> agent is pressured to activate the rejected candidate before making a new
   candidate edit
```

That pressure is not the desired long-term workflow. A cancelled or rejected
reload means the candidate must not become active merely so the agent can
continue. The current safe behavior is still to block normal GPU work while
stale, but the recovery design should allow the rejected candidate to be
discarded or superseded without activating it.

Desired follow-up behavior:

- `reject_policy_reload` invalidates the pending token and leaves the active
  policy unchanged;
- stale-policy blocking continues to refuse normal GPU/cluster work;
- the recovery surface should include a narrow way to discard or supersede the
  inactive candidate, or should clearly route that step to a human file edit;
- any superseded candidate invalidates older reload tokens and requires a fresh
  `preview_policy_reload`;
- the agent must not call `reload_policy` for a rejected/cancelled candidate
  unless the human explicitly approves that exact preview again.

## Modality 4: Headless Codex Hook Behavior

This was tested with `codex exec` to understand what hooks can and cannot do in
headless mode.

Observed behavior:

Manual experiment evidence from this session, not deterministic pytest coverage:

- a `PostToolUse` hook can block or replace the just-finished tool result;
- the model may still produce a final explanatory message after the hook fires;
- registering the same policy check as `PreToolUse` blocks subsequent normal
  tools before they execute while the policy is stale;
- therefore the repo config uses both `PreToolUse` and `PostToolUse`;
- the hook is not the security boundary. The server's stale-policy refusal is
  the reliable backstop if a client skips hooks or has different hook behavior.

This means the tested guarantee is not "headless Codex instantly terminates the
process." The tested guarantee is narrower and more useful:

```text
once gpu-mcp.toml is stale against the active server hash, further normal MCP
tools are blocked by the server's stale-policy check; when the file also no
longer matches the external approval store, the Codex hook gives earlier
workflow feedback
```

## Modality 5: Approval Scope

The current design keeps the approval boundary narrow:

- `preview_policy_reload` is non-activating and should remain callable while stale;
- `reject_policy_reload` only discards a pending token and should remain callable
  while stale;
- `reload_policy` is the policy activation point. The server cannot prove human
  intent by itself, so Codex config must set this tool to
  `approval_mode = "prompt"`;
- the `reload_policy` prompt is visible only in the human Codex UI. Once the
  human approves it, the agent sees the ordinary MCP result and cannot tell from
  that result that approval happened;
- ADR 0001-sensitive tools such as `run_python_on_gpu` and `kill_gpu_process`
  keep their own approval/safety treatment, but they are not the approval
  checkpoint for ADR 0002.

The repo-local `.codex/config.toml` and setup docs put the required human
prompt on `reload_policy`. `preview_policy_reload` and `reject_policy_reload`
are not the security checkpoint, although a client UI may still ask the human to
confirm those tool calls as conservative friction.

## Final Acceptance Checks

These are not extra edge tests. They are the concrete checks that close the
policy-edit workflow.

Interactive approval UI:

```text
1. Start a fresh trusted Codex session after `.codex/config.toml` is loaded.
2. Make a harmless `gpu-mcp.toml` edit.
3. Confirm the hook fires.
4. Call `preview_policy_reload`.
5. If Codex prompts for preview, approve it and continue; preview is
   non-activating.
6. Show the raw preview output to the human.
7. Call `reload_policy(token=...)`.
8. Confirm Codex shows the approval prompt for reload before activation.
9. Approve reload.
10. Confirm a normal MCP tool works again.
```

This cannot be proven by direct MCP calls from this assistant session, because
direct tool calls bypass Codex's tool-approval UI. It must be observed through
the actual interactive Codex surface.

Fresh install:

```text
1. Run the installer/doctor flow in a new repo or account.
2. Confirm it writes the repo-local hook config.
3. Confirm it puts the required human prompt on `reload_policy`.
4. Confirm `preview_policy_reload` and `reject_policy_reload` remain callable
   recovery tools; any client prompt on them is extra friction, not the policy
   activation checkpoint.
5. Run the same harmless edit -> preview -> approved reload path.
```

Headless hook behavior was checked during this session. Re-check it when Codex
hook semantics change; that is maintenance, not an unimplemented feature.

## Current Judgment

For policy config editing, the important feature path is implemented and tested:

```text
active-hash stale detection -> normal-tool refusal -> non-activating preview
-> exact-hash reload -> external human approval checkpoint at reload
-> recovery/cleanup path
```

More adversarial edge tests can be added later, but the highest-value remaining
work is to run the final acceptance checks above in the actual surfaces they
depend on.
