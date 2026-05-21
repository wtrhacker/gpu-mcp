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

Required checks:

- config parsing and path policy;
- dedicated-key SSH for configured hosts;
- `nvidia-smi` and remote repo visibility on reachable hosts;
- remote `realpath(repo_root)`, Python executable, probe script hash, working
  directory, and minimal imports;
- repo-local Codex MCP config with real `codex exec` probe;
- global Codex execpolicy prompt rules for `ssh`, `scp`, `sftp`, and `rsync`;
- blocked `codex exec` probes for those commands when approvals are disabled;
- no raw-command probe may include `--ignore-rules`;
- client `tool_timeout_sec > sync_timeout_sec`.

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
