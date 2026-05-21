# GPU MCP Install Instructions For Codex

This file is for the Codex agent installing GPU MCP into a research repo.

Rules:

- Do not edit global Codex config with repo-specific paths.
- Use repo-local `.codex/config.toml`.
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
11. Run doctor checks and a real `codex exec` MCP probe.
12. Update `progress.md` after each completed step or blocker.
