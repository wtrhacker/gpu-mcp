# GPU MCP Install Instructions For Codex

This file is for the Codex agent installing GPU MCP into a research repo.

Rules:

- Do not edit global Codex config with repo-specific paths.
- Use repo-local `.codex/config.toml`.
- Use exactly one MCP server name for this implementation:
  `gpu-cluster-mcp`.
- Repo-specific behavior must come from the `--config` path, not from changing
  the MCP server name.
- Do not reuse or edit the legacy `gpu-cluster` MCP entry during migration.
- Start the MCP server with explicit `--config /absolute/path/to/gpu-mcp.toml`.
- Verify global Codex execpolicy prompt rules for raw remote commands: `ssh`,
  `scp`, `sftp`, and `rsync`.
- Verify noninteractive GPU-MCP automation runs Codex with approvals disabled,
  so those prompt-required commands are rejected.
- Never use `--ignore-rules` for install validation or GPU-MCP work. That flag
  disables the execpolicy rules being verified.
- Treat bootstrap inventory as SSH reachability evidence, not permission.
- Ask the human before writing safety policy values into `gpu-mcp.toml`.
- Use doctor JSON and MCP probe outputs as evidence; `progress.md` is only a
  journal.
- A local MCP probe is only an optional smoke check. It must not be counted as
  install acceptance.
- Acceptance requires the full real battlefield suite through `codex exec` and
  the real installed MCP, using non-local hosts verified by bootstrap.
- The repeatable acceptance test is:
  `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py`.

Install flow:

1. Confirm the human already ran SSH bootstrap with explicit hostnames or a
   hosts file.
2. Read `~/.cache/gpu-mcp/bootstrap_hosts.json`.
3. Confirm the research repo root with the human.
4. Propose `nodes` as a subset of verified bootstrap hosts.
5. Propose `script_roots`, `write_roots`, and `output_roots`.
6. Ask the human to approve repo root, hosts, script roots, and write roots.
7. Write repo-local `gpu-mcp.toml`.
8. Run `gpu_mcp_doctor.py approve-policy --config <repo>/gpu-mcp.toml --yes`
   after the human approves the proposed policy.
9. Write repo-local `.codex/config.toml`.
10. Ensure `run_python_on_gpu` and `kill_gpu_process` use
   `approval_mode = "approve"`, while `reload_policy` uses
   `approval_mode = "prompt"`. For the policy-edit flow, `reload_policy` is
   the human approval checkpoint; Codex shows that prompt only in the human UI,
   and the agent only sees the MCP result after approval. `preview_policy_reload`
   and `reject_policy_reload` should remain callable for recovery while stale.
11. Add the repo-local `PreToolUse` and `PostToolUse` hooks that run
    `gpu_mcp_policy_hook.py`, then ask the human to review and trust them in
    Codex. Treat the hooks as agent guidance; the server remains the policy
    boundary.
12. Verify raw remote command prompt rules and blocked `codex exec` probes
    without `--ignore-rules`.
13. Optionally run a local smoke probe through MCP to catch config mistakes.
14. Run doctor checks.
15. Run the full real battlefield suite through real `codex exec` and the real
    installed MCP.
16. Treat the install as incomplete if any designed battlefield policy family
    lacks a passing wet test or a documented reason it cannot be safely run.
17. Update `progress.md` after each completed step or blocker.

During ordinary long-horizon GPU work, do not edit `gpu-mcp.toml` or
`.codex/config.toml` to bypass a policy rejection. If the human explicitly asks
for a policy change, treat that as a separate policy-edit task: propose the
diff, call `preview_policy_reload`, show the raw preview output including
`diff_summary` and hashes, get explicit approval, and call `reload_policy`
before continuing GPU work. If the human rejects the candidate, call
`reject_policy_reload` and stop that policy-edit task.
