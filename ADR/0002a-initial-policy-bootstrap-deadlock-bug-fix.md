# Bug Fix 0002a: Initial Policy Bootstrap Deadlock

## Status

Implemented and verified on 2026-08-30.

## Relationship to ADR 0002

This is an implementation bug-fix note associated with
[`0002-policy-edit-and-reload-harness.md`](0002-policy-edit-and-reload-harness.md).
It does not rewrite that historical decision record.

The preserved security invariant is:

> Never use an unapproved policy.

The bug was that the implementation also terminated the policy-recovery
control plane, making the invariant impossible to satisfy from a new repo.

## Failure

The first-policy sequence could enter a permanent deadlock:

1. An agent or human wrote `gpu-mcp.toml` for a new research repo.
2. The companion hook saw that no approval record existed and blocked normal
   tools. It allowed the policy preview, reload, and rejection tools.
3. The MCP server checked the same approval record during import and exited
   before FastMCP registered those recovery tools.
4. The documented `gpu_mcp_doctor.py approve-policy --yes` shell command was
   also blocked by the hook.

The hook therefore allowed recovery calls that could never exist, while the
server required an approval that the agent could not safely obtain.

## Corrected Behavior

The server now has a fail-closed `bootstrap_pending` state.

- A missing, invalid, unapproved, or changed-at-start policy does not become
  active authority.
- The stdio MCP process remains alive so policy preview, rejection, and prompted
  activation are reachable.
- All seven operational tools share one server-side capability gate. Before an
  approved policy is active, they refuse before SSH, GPU inspection, process
  access, reservation access, log writes, or managed-job work.
- The tools may remain visible in the MCP list so activation can unlock them in
  the same client session; visibility does not bypass the central gate.
- A symlinked policy remains a hard startup error.

Initial `preview_policy_reload` is read-only with respect to approval state. It
returns:

- `approval_state = "bootstrap_pending"`;
- `activation_mode = "bootstrap"`;
- `active_hash = null`;
- the exact candidate hash;
- the complete canonical `candidate_summary`;
- an explicit no-active-baseline diff;
- raw agent instructions; and
- a one-time token bound to the config path, candidate hash, active-state hash,
  and server instance.

`reload_policy` remains the only activation point. Repo-local Codex config must
set it to `approval_mode = "prompt"`. After the human reviews the raw preview
and approves the call, the server reads one immutable byte snapshot whose hash
and canonical summary therefore cannot describe different file versions. It
records approval only if the current validated snapshot still has the expected
preview hash, rechecks the file, installs that exact policy in memory, and
clears the bootstrap state. A concurrent edit cannot poison the approval store
with an unreviewed hash. Wrong, expired, reused, cross-instance, or post-edit
tokens refuse. Rejection consumes the token and leaves quarantine in place.

## Hook and Installer Recovery

The hook now:

- distinguishes first activation from later stale-policy drift;
- permits only exact canonical GPU MCP recovery tool names rather than any name
  with a matching suffix;
- keeps narrow `gpu-mcp.toml` repair available; and
- during first bootstrap only, permits path-limited creation or repair of this
  repo's `.codex/config.toml` so a policy-first setup is recoverable.

That config exception refuses a symlinked `.codex` directory or config file, so
it cannot redirect the write outside the repo. It limits the destination path,
not the TOML contents. The human must inspect the complete project config
before trusting the repo or restarting Codex.

The AI-native installer uses a config-first order: write the repo MCP
registration, write the candidate policy, start or restart Codex from the
trusted repo, show the raw preview, obtain explicit human approval, and call the
prompted activation tool. The normal agent workflow no longer invokes
`gpu_mcp_doctor.py approve-policy --yes`; that command remains an administrator
recovery interface, not proof of a human-approved agent action.

## Verification

Deterministic policy and hook coverage includes missing, invalid, unapproved,
and changed-at-start policy; complete initial preview; rejection; token
activation; same-session unlock; all operational entry points; exact hook
allowlisting; config symlink rejection; parse/hash race resistance;
approval-store race resistance; and path-limited Codex config repair.

Verified commands and results:

```text
pytest -q test/test_gpu_mcp_policy_reload_contract.py \
  test/test_gpu_mcp_policy_hook_contract.py
89 passed

pytest -q -m contract
291 passed, 71 deselected

pytest -q
318 passed, 44 skipped

GPU_MCP_RUN_CODEX_EXEC_TESTS=1 \
  pytest -q test/test_codex_policy_bootstrap.py
2 passed
```

The two live tests start real fresh Codex agents against the real MCP server.
They prove that an unapproved first policy is previewable without an
out-of-band approval record and that an operational call receives the
server-side bootstrap refusal without creating that record.

Automated tests verify the mechanics around human approval. They do not claim
to simulate the human's judgment in the interactive approval UI.
