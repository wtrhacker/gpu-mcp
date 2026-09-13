# GPU MCP Installation Progress

**Copy this template to `progress.md` in the research repo root before starting.**
Fill it in as you complete each step. This file is a journal, not proof. The
real evidence is doctor JSON output and test results.

## Basic Info

| Item | Value |
|------|-------|
| Research repo root | |
| GPU MCP runtime root | |
| GPU MCP server path (`<runtime>/gpu_mcp_server.py`) | |
| Control host (where you are running) | |
| Server Python executable | |
| GPU job Python executable | |
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
- [ ] Human identified the exact installed runtime root and server Python
- [ ] Verified the runtime files exist and server Python imports MCP SDK v1,
  Fabric, and Paramiko
- [ ] Confirmed the research repo root with the human
- [ ] Human ran SSH bootstrap with explicit hostnames or `--hosts-file`
- [ ] Read bootstrap inventory and found at least one verified non-local host

### Codex bootstrap integration
- [ ] Wrote repo-local `.codex/config.toml`
- [ ] Verified repo-local `.codex/config.toml` contains no GPU MCP hook blocks
- [ ] Set `tool_timeout_sec` > `sync_timeout_sec`
- [ ] Configured tool approval modes:
  - [ ] `run_python_on_gpu` = `"approve"`
  - [ ] `check_gpus` = `"approve"`
  - [ ] `kill_gpu_process` = `"approve"`
  - [ ] `check_gpu_processes` = `"approve"`
  - [ ] `cluster_info` = `"approve"`
  - [ ] `manage_gpu_job` = `"approve"`
  - [ ] `list_gpu_reservations` = `"approve"`
  - [ ] `preview_policy_reload` = `"approve"`
  - [ ] `reject_policy_reload` = `"approve"`
  - [ ] `reload_policy` = `"prompt"`
- [ ] Installed user-global GPU MCP companion hook (`PreToolUse`, `PostToolUse`,
  and `Stop` in Codex config)
- [ ] Human trusted the global GPU MCP hook in Codex (ran `/hooks` or equivalent)
- [ ] Restarted Codex from the repo and trusted its project config
- [ ] Confirmed the MCP starts in `bootstrap_pending` when no policy is active

### Policy proposal and activation
- [ ] Confirmed research repo root with the human
- [ ] Proposed GPU host list to the human (subset of verified hosts)
- [ ] Proposed `script_roots`, `write_roots`, and `output_roots` to the human
- [ ] Wrote the proposed `gpu-mcp.toml` to the repo root
- [ ] Repaired only `gpu-mcp.toml` until `preview_policy_reload` validated it
- [ ] Saved the complete raw preview response
- [ ] Showed the human `candidate_summary`, `diff_summary`, `active_hash`, and
  `candidate_hash` without replacing them with an agent summary
- [ ] Human inspected the candidate file and explicitly approved that preview
- [ ] Called prompted `reload_policy` with the preview's one-time token
- [ ] Verified `approval_state = "active"` and active hash = candidate hash
- [ ] Did not use doctor `approve-policy --yes` as the normal install path

### Verification
- [ ] Verified raw remote command blocking (SSH, scp, rsync, codex spawn)
- [ ] Ran focused installation test with explicit runtime/Python paths
- [ ] Ran doctor check with `--json`; required local checks passed and any
  skipped live checks were reported honestly
- [ ] If requested, ran a harmless live GPU launch proof on an explicit host
- [ ] Did not treat the maintainer battlefield suite as a normal install step
- [ ] Verified managed-job hook reminders and status continuation work
- [ ] Updated this progress file

## Proposed Values

Paste the exact `gpu-mcp.toml` candidate that was previewed and approved:

```toml
# Paste proposal here
```

- Preview candidate hash: _________________________________________________
- Active policy hash: ____________________________________________________
- Human approved on: _____________________________________________________

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
| Focused install | `installation_test.py --repo ... --mcp-root ... --python ... --json` | | |
| Optional live launch | `installation_test.py ... --live-gpu --host ...` | | |

## Notes

Anything else worth remembering for the next session:
