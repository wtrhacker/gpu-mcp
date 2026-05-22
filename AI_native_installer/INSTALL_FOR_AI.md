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
- Acceptance requires a full remote end-to-end `codex exec` probe through the
  real installed MCP, using a non-local host verified by bootstrap.

Install flow:

1. Confirm the human already ran SSH bootstrap with explicit hostnames or a
   hosts file.
2. Read `~/.cache/gpu-mcp/bootstrap_hosts.json`.
3. Confirm the research repo root with the human.
4. Propose `nodes` as a subset of verified bootstrap hosts.
5. Propose `script_roots`, `write_roots`, and `output_roots`.
6. Ask the human to approve repo root, hosts, script roots, and write roots.
7. Write repo-local `gpu-mcp.toml`.
8. Write repo-local `.codex/config.toml`.
9. Ensure `run_python_on_gpu` uses `approval_mode = "approve"` for
   noninteractive `codex exec` probes.
10. Verify raw remote command prompt rules and blocked `codex exec` probes
    without `--ignore-rules`.
11. Optionally run a local smoke probe through MCP to catch config mistakes.
12. Run doctor checks.
13. Run the required remote acceptance probe through real `codex exec` and the
    real installed MCP.
14. Run at least one policy-rejection probe, such as a job that attempts to
    write outside approved write roots.
15. Update `progress.md` after each completed step or blocker.
