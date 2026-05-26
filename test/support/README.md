# Test Support Files

This directory holds small test-only helpers and notes used by the Codex/GPU MCP
test strategy.

- `proxy_for_codex_exec.py`: local MCP proxy used to prove repo-local Codex MCP
  loading and explicit `--config` handling without SSH or GPUs.
- `test_proxy_for_codex_exec.py`: deterministic unit coverage for the proxy.
- `kill_policy_contract_helper.py`: deterministic stand-in for kill-process
  safety contract tests.
- `CODEX_REPO_LOCAL_MCP_NOTES.md`: historical evidence for repo-local MCP
  loading through `codex exec`.
- `CODEX_EXECPOLICY_NOTES.md`: historical evidence for raw remote-command
  blocking through Codex execpolicy.
- `POLICY_EDIT_MODALITIES.md`: recent evidence for `gpu-mcp.toml` edit,
  stale-policy, preview/reload, and hook behavior.

These files support the normal contract tests and the opt-in live battlefield
suite documented in `../BATTLEFIELD.md`. They are not production
implementations.
