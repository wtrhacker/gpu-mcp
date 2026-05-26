# Real Battlefield Tests

`test/test_real_gpu_mcp_battlefield.py` is the live acceptance layer for GPU MCP.
It proves the behavior that cannot be reduced to normal unit or contract tests:
real `codex exec`, repo-local MCP configuration, the real MCP server, SSH to
verified non-local hosts, shared `/net` repos, and actual GPU framework use.

The suite is intentionally not a complete assertion-by-assertion spec. Read it
as a battlefield: it asks an AI agent to use the MCP boundary under realistic
conditions, then checks that the server, Codex policy, and remote guardrails hold
together.

## What It Proves

The live suite currently covers these strategy-level guarantees:

- Codex can load a repo-local `.codex/config.toml` and call the same MCP server
  name with repo-specific `gpu-mcp.toml` policy.
- A job runs on a verified non-local host, receives the selected
  `CUDA_VISIBLE_DEVICES`, and can use a GPU through JAX or Torch.
- The installed control-side server can use the repo-staged safe runner when
  the control-side server path is not visible on the remote host, as long as the
  research repo itself is on shared `/net`.
- Separate repos stay isolated even when they use the same MCP server name.
- Hosts outside a repo policy are rejected for both run and process-inspection
  tools, without allowing Codex to mutate policy or client config as an escape.
- Script roots, write roots, explicit `/tmp` write roots, output roots,
  symlinks, SQLite writes, `dir_fd` writes, and adversarial argv strings are
  treated as policy boundaries rather than shell text to reinterpret.
- Async launches write only to approved output roots and do not overwrite source
  scripts.
- The remote safe runner rejects Python remote-control APIs such as subprocess,
  `os.system`, dynamic subprocess import, sockets, and `ctypes`.
- Sync timeout behavior is enforced by the server boundary.
- Raw remote commands such as `ssh`, `scp`, `sftp`, and `rsync` are blocked by
  Codex execpolicy when the probe is run without `--ignore-rules`.
- `kill_gpu_process` is a two-step inspect-and-fingerprint flow and only signals
  the harmless owned fixture process after the inspected fingerprint is replayed.

## Why It Is Skipped By Default

Every test is marked `codex_exec` and `real_battlefield`, and the module-level
skip requires:

```bash
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -q test/test_real_gpu_mcp_battlefield.py
```

The default skip is deliberate. The suite depends on current site state: Codex
CLI behavior, execpolicy rules, SSH access, shared `/net` visibility, installed
Python packages on remote hosts, GPU availability, and the bootstrap inventory.
It also creates and removes a battlefield fixture tree and intentionally launches
remote processes. It is acceptance evidence, not a routine presubmit test.

Several tests have additional runtime skips:

- the installed-server staging test skips if `GPU_MCP_INSTALLED_SERVER` does not
  exist;
- the same test skips if that installed server path is already visible on the
  remote host, because then it cannot prove the staged-runner fallback;
- host-rejection and protected policy-mutation tests skip unless the bootstrap
  inventory has at least two verified non-local hosts.

## Prerequisites

Before enabling the suite, the environment must provide:

- `codex` on `PATH`.
- An MCP-capable Python with the project dependencies available.
- SSH key `~/.ssh/gpu_mcp_key` accepted by the verified hosts.
- Bootstrap inventory at `~/.cache/gpu-mcp/bootstrap_hosts.json`, or an explicit
  `GPU_MCP_BOOTSTRAP_INVENTORY`.
- At least one `status = verified` non-local host in that inventory. Tests that
  need a rejected host require two verified non-local hosts.
- Shared `/net` access from the control machine and remote host to the
  battlefield fixture root.
- Remote GPU framework support through JAX or Torch for the framework probe.
- Codex execpolicy prompt rules for raw remote commands, validated without
  `--ignore-rules`.

Useful environment variables:

- `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1`: opt in to the live suite.
- `GPU_MCP_BOOTSTRAP_INVENTORY`: override the bootstrap inventory path.
- `GPU_MCP_BATTLEFIELD_ROOT`: override the test-owned fixture root. The path is
  safety-checked and must clearly contain `gpu-mcp-battlefield`.
- `GPU_MCP_PYTHON`: Python used by repo-local Codex MCP config.
- `GPU_MCP_INSTALLED_SERVER`: installed control-side server path for the staged
  runner test. Defaults to `~/gpu-mcp/gpu_mcp_server.py`.
- `GPU_MCP_CODEX_MODEL`: Codex model for battlefield prompts. Defaults to
  `gpt-5.5`.
- `GPU_MCP_CODEX_REASONING`: Codex reasoning effort. Defaults to `high`.

## Generated Fixtures

The pytest fixture writes two temporary repos under the battlefield root:
`repo_a` and `repo_b`. Each repo gets its own `gpu-mcp.toml`,
`.codex/config.toml`, job scripts, `results`, and `.gpu_mcp_logs`. The root also
contains `outside_sentinels` used to prove that rejected writes did not escape.

The fixture removes the battlefield root at setup time. Do not point
`GPU_MCP_BATTLEFIELD_ROOT` at a human-owned directory.

## Support Files

`test/support/proxy_for_codex_exec.py` is a small local MCP server for
deterministic Codex/repo-local tests. It does not SSH or inspect GPUs. Its job is
to prove that Codex loads repo-local MCP config and passes an explicit
`--config` path into a server with the same tool-call shape as production.

`test/support/test_proxy_for_codex_exec.py` unit-tests that proxy and keeps the
two-repo policy behavior reproducible without live hosts.

`test/support/kill_policy_contract_helper.py` is a deterministic helper for
kill-process policy contract tests. It mirrors the intended inspect,
fingerprint, owner-check, and signal behavior without using real processes.

`test/support/CODEX_REPO_LOCAL_MCP_NOTES.md` and
`test/support/CODEX_EXECPOLICY_NOTES.md` are experiment notes. They explain why
the test strategy uses actual `codex exec` probes instead of trusting
`codex mcp list`, and why raw remote-command controls are treated as practical
Codex execpolicy evidence rather than a permanent hard-deny guarantee.

## Still Manual Or Interactive

Some acceptance evidence remains human-supervised:

- Installing or refreshing Codex execpolicy rules and confirming they still
  block raw remote commands on the current Codex version.
- Reviewing bootstrap inventory health and choosing suitable verified hosts.
- Interpreting failures caused by site load, unavailable GPUs, package drift, or
  SSH/key problems.
- Confirming interactive Codex behavior when approvals are enabled. Most probes
  run non-interactive `codex exec --ask-for-approval never` with
  `--dangerously-bypass-hook-trust` so hook trust prompts do not block
  automated acceptance. The raw remote-command probe intentionally omits that
  bypass and validates execpolicy without `--ignore-rules`.
- Verifying installer/doctor UX end to end. The battlefield suite validates the
  live boundary, not every installation path or ADR002 scenario.
