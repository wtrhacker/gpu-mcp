# GPU MCP Installation Progress

Research repo:
GPU MCP server path:
Control host:
Python executable:
MCP config path:
Dedicated SSH key:
Bootstrap inventory:
Configured hosts:

## Checklist

- [ ] Human ran SSH bootstrap with explicit hostnames or `--hosts-file`
- [ ] User-level bootstrap inventory exists
- [ ] At least one non-local host is verified in the bootstrap inventory
- [ ] Confirm research repo root with human
- [ ] Confirm allowed GPU hosts with human
- [ ] Confirm script roots with human
- [ ] Confirm write roots with human
- [ ] Write repo-local `gpu-mcp.toml`
- [ ] Human-approved policy hash recorded with `gpu_mcp_doctor.py approve-policy`
- [ ] Python imports pass
- [ ] Server config validation passes
- [ ] Dedicated-key SSH verification passes
- [ ] Remote repo path is visible on at least one non-local host
- [ ] `nvidia-smi` works on at least one non-local host
- [ ] Repo-local Codex MCP config installed
- [ ] Codex restarted or reloaded after MCP config change
- [ ] `codex exec` probe sees and can call the repo-local MCP tool
- [ ] Optional local smoke probe succeeded through MCP, if run
- [ ] Full real battlefield suite succeeded through real `codex exec`
- [ ] Every designed battlefield policy family is covered or explicitly marked unsafe to run
- [ ] `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py` passes

## Blockers

None yet.

## Probe Results

Local:
Remote:
Policy rejection:
