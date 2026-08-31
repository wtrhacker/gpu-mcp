# GPU MCP Doctor CLI Contract

Doctor is an install-readiness checker for the installer agent. It should not
silently install SSH keys, edit global Codex config, broaden roots, or run
arbitrary user scripts.

Initial commands:

```bash
python gpu_mcp_doctor.py check --config /absolute/path/to/repo/gpu-mcp.toml
python gpu_mcp_doctor.py check --config /absolute/path/to/repo/gpu-mcp.toml --json
```

`--config` is required and must be absolute. Doctor derives `repo_root` from
that policy file, then verifies the policy location and repo-local Codex config
match the same repo. v1 should not expose separate `preflight`, `ssh`,
`nvidia`, or `mcp-config` public commands; those are internal check names in
the JSON result.

Default `check` output must be honest about what it did and did not prove. It
always validates local config, approval state, repo-local Codex config, and
timeout alignment. Live remote/Codex checks that require site state should be
reported as `skip` unless the caller supplies the needed probe inputs or runs a
full acceptance flow.

Readiness checks:

- config parsing and path policy;
- dedicated-key SSH for configured hosts;
- `nvidia-smi` and remote repo visibility on reachable hosts;
- remote `realpath(repo_root)`, Python executable, probe script hash, working
  directory, and minimal imports;
- repo-local Codex MCP config with real `codex exec` probe;
- global Codex execpolicy prompt rules for `ssh`, `scp`, `sftp`, `rsync`, and
  `codex`;
- blocked `codex exec` probes for those commands when approvals are disabled;
- no raw-command probe may include `--ignore-rules`;
- client `tool_timeout_sec > sync_timeout_sec`.

Doctor is not the normal AI-native policy activation route. The agent must
configure the repo-local MCP first, let the server enter `bootstrap_pending`
quarantine, and use `preview_policy_reload`. The complete raw preview must be
shown to the human, including `candidate_summary`, `diff_summary`,
`active_hash`, and `candidate_hash`. Only after explicit human approval may the
agent call the client-prompted `reload_policy` with the one-time preview token.
That call writes the approved hash and audit history and unlocks operations in
the running server.

The CLI may retain `approve-policy` as an administrator recovery command, but
an installer agent must not use `approve-policy --yes` to bypass the MCP
preview and prompted activation ceremony. A missing, invalid, unapproved, or
changed-at-start policy does not need that workaround: the MCP server remains
available in bootstrap quarantine, all operational tools are blocked, and the
policy can be repaired and previewed through the intended recovery surface.

Doctor JSON should be stable:

```json
{
  "schema_version": 1,
  "status": "fail",
  "config_path": "/abs/repo/gpu-mcp.toml",
  "repo_root": "/abs/repo",
  "generated_at": "2026-05-21T00:00:00-04:00",
  "checks": [
    {
      "name": "remote_repo_visible",
      "status": "fail",
      "host": "gpu01.example.edu",
      "message": "/abs/repo not found on remote host",
      "details": {
        "probe": "realpath"
      }
    }
  ]
}
```
