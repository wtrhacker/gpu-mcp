# GPU MCP Installation Progress

**Copy this template to `progress.md` in the research repo root before starting.**
Fill it in as you complete each step. This file is a journal, not proof. The
real evidence is doctor JSON output and test results.

## Basic Info

| Item | Value |
|------|-------|
| Research repo root | |
| GPU MCP install path (e.g. `~/gpu-mcp`) | |
| GPU MCP server path (e.g. `~/gpu-mcp/gpu_mcp_server.py`) | |
| Control host (where you are running) | |
| Python executable used | |
| MCP config path | |
| Dedicated SSH key | `~/.ssh/gpu_mcp_key` (created by bootstrap) |
| `tool_timeout_sec` | Must be > `sync_timeout_sec` (e.g. 360 vs 300) |

## Verified Hosts

List hosts from `~/.cache/gpu-mcp/bootstrap_hosts.json` with `"status": "verified"`:

- [ ] Host 1:
- [ ] Host 2:
- [ ] Host 3:

If no hosts are verified, stop and tell the human.

## Installation Checklist

### Human prerequisites
- [ ] Human ran SSH bootstrap with explicit hostnames or `--hosts-file`
- [ ] Read bootstrap inventory and found at least one verified non-local host

### Policy setup
- [ ] Confirmed research repo root with the human
- [ ] Proposed GPU host list to the human (subset of verified hosts)
- [ ] Proposed `script_roots` to the human
- [ ] Proposed `write_roots` to the human
- [ ] Human approved all proposed values
- [ ] Wrote `gpu-mcp.toml` to repo root
- [ ] Ran `approve-policy --yes` and it succeeded

### Codex integration
- [ ] Wrote repo-local `.codex/config.toml`
- [ ] Verified repo-local `.codex/config.toml` contains no GPU MCP hook blocks
- [ ] Set `tool_timeout_sec` > `sync_timeout_sec`
- [ ] Configured tool approval modes:
  - [ ] `run_python_on_gpu` = `"approve"`
  - [ ] `kill_gpu_process` = `"approve"`
  - [ ] `check_gpu_processes` = `"approve"`
  - [ ] `reload_policy` = `"prompt"`
- [ ] Installed user-global GPU MCP companion hook (`PreToolUse`, `PostToolUse`,
  and `Stop` in Codex config)
- [ ] Human trusted the global GPU MCP hook in Codex (ran `/hooks` or equivalent)

### Verification
- [ ] Verified raw remote command blocking (SSH, scp, rsync, codex spawn)
- [ ] Ran full doctor check with `--json`, all required checks passed
- [ ] Ran full battlefield suite and it passed
- [ ] Updated this progress file

### 004 extension placeholder
When multi-agent GPU coordination is enabled, add checkboxes here:
- [ ] Configured `check_gpus` approval mode
- [ ] Configured `manage_gpu_job` approval mode
- [ ] Configured `list_gpu_reservations` approval mode
- [ ] Verified hook heartbeat reminders work

## Proposed Values

Paste the actual `gpu-mcp.toml` you proposed and the human approved:

```toml
# Paste proposal here
```

- Human approved on: _______________

## Blockers

List anything that stopped or slowed the install:

| Step | What happened | What you tried | Status |
|------|--------------|----------------|--------|
| Example: SSH bootstrap | Host `gpu01` timed out | Re-ran with `-v`; same result | Waiting for human |
| | | | |

## Test Results

| Test | Command | Result | Notes |
|------|---------|--------|-------|
| Doctor JSON | `gpu_mcp_doctor.py check --config ... --json` | | |
| Battlefield | `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest ...` | | |

## Notes

Anything else worth remembering for the next session:
