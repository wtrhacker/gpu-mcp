# GPU MCP Test Suite

The test suite is a first-class part of GPU MCP's design. The project is not
only a Python package; it is a safety boundary around AI-driven remote GPU
work. The tests therefore check the control-plane contracts, the installer
surface, Codex integration points, and a real adversarial battlefield path. That
elaborate coverage is a feature of the project, not incidental scaffolding.

## Philosophy

Use the cheapest layer that proves the behavior:

- Deterministic pytest contracts cover config parsing, policy boundaries,
  schema shape, safe-run behavior, doctor checks, and reload mechanics.
- Local Codex/MCP fixture tests prove repo-local `.codex/config.toml` selection
  without touching real GPUs.
- Raw remote-command tests validate the Codex execpolicy assumptions used by
  the installer flow.
- The real battlefield suite is reserved for end-to-end acceptance: real Codex,
  the real MCP server, real SSH, shared `/net` fixture repos, and remote GPU
  execution.

Default tests should stay local and fixture-owned. They must not require real
GPU hosts, real SSH keys, or user/system state. Tests that need those resources
are opt-in and visibly marked.

## Commands

Run the normal suite:

```bash
pytest
```

Run contract tests only:

```bash
pytest -m contract
```

Run a focused file while developing:

```bash
pytest test/test_gpu_mcp_server_contract.py
```

Run live repo-local Codex probes:

```bash
GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -m codex_exec test/test_codex_repo_local_mcp.py
```

Run the real battlefield suite:

```bash
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -m "codex_exec and real_battlefield" test/test_real_gpu_mcp_battlefield.py
```

The battlefield suite also reads site state such as the bootstrap inventory,
dedicated SSH key, shared fixture root, Python path, and Codex model settings.
See the constants and environment overrides at the top of
`test/test_real_gpu_mcp_battlefield.py` before running it.

## Skipped By Default

- `test/test_codex_repo_local_mcp.py` has three live `codex exec` tests skipped
  unless `GPU_MCP_RUN_CODEX_EXEC_TESTS=1` is set. Its pure local config tests
  still run by default.
- `test/test_real_gpu_mcp_battlefield.py` is skipped unless
  `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1` is set. It intentionally uses real
  Codex, SSH, non-local hosts from the bootstrap inventory, and shared `/net`
  repos.

`pytest.ini` registers the `contract`, `codex_exec`, and `real_battlefield`
markers, sets `test/` as the test root, excludes `STALE/`, and uses strict
xfail handling.

## Environment And Fixture Roots

Most deterministic tests run with `GPU_MCP_TEST_DISABLE_POLICY_APPROVAL=1` set
by `test/conftest.py` so policy-approval state does not leak in from a real
install. The server only honors this bypass while running under pytest. Reload
contract tests isolate the approval store by monkeypatching the approval module,
not by passing a production environment variable into the server.

Local fixture repos are generated under `test_mcp_repos/`. The real battlefield
suite uses its own opt-in shared fixture root, documented in
`test/BATTLEFIELD.md`.

`test/support/POLICY_EDIT_MODALITIES.md` records the evidence from recent
policy-edit and policy-reload modality checks. Read it as a narrow acceptance
log for `gpu-mcp.toml` edits, stale-policy blocking, preview/reload, and hook
behavior.

## File Map

- `test/conftest.py`: puts the repo root and `test/` on `sys.path`, which makes
  support modules importable, and disables policy approval by default for
  deterministic tests.
- `test/test_contract_templates.py`: validates template JSON files against the
  contract schemas.
- `test/test_gpu_mcp_bootstrap_contract.py`: human-first SSH bootstrap,
  inventory shape, and the rule that bootstrap evidence is not runtime
  permission.
- `test/test_gpu_mcp_doctor_contract.py`: AI-facing install doctor CLI,
  repo-local Codex config validation, policy approval records, timeout
  alignment, and Codex/MCP probe construction.
- `test/test_installation_test_contract.py`: focused installer-facing readiness
  command for a target repo, including static setup, mandatory Codex exec probe
  construction, and explicit live GPU probe construction without running live
  resources by default.
- `test/test_gpu_mcp_policy_contract.py`: repo-local policy parsing and
  deterministic host, script-root, write-root, output-root, timeout, and
  Python-only guard behavior.
- `test/test_gpu_mcp_policy_reload_contract.py`: ADR 0002 coverage for approved
  policy records, first-policy bootstrap quarantine, the central operational
  capability gate, preview/reload/reject tokens, stale-policy refusal, token
  expiry, and token cap behavior.
- `test/test_gpu_mcp_policy_hook_contract.py`: deterministic coverage for the
  PreToolUse/PostToolUse policy checks, active managed-job reminders, Stop
  waiter, recovery-tool allowlist, and runtime instructions.
- `test/ADR0005_INTERACTIVE_HOOK_RESULTS.md`: human-supervised live evidence
  for active and waiting managed-job notification in an interactive Codex
  session.
- `test/test_gpu_mcp_server_contract.py`: server startup, safe-run boundaries,
  staged remote runner behavior, guard edge cases, output symlink handling,
  async PID validation, kill argument validation, SSH key checks, and host alias
  behavior. This is the main deterministic coverage for ADR 0003.
- `test/test_gpu_mcp_kill_contract.py`: safe `kill_gpu_process` inspection,
  ownership checks, fingerprint confirmation, and signaling contract.
- `test/test_codex_global_command_ban_contract.py`: global Codex raw remote
  command policy for `ssh`, `scp`, `sftp`, and `rsync`, including probe argv
  validation and obvious bypass-form reporting.
- `test/test_codex_policy_edit_contract.py`: harness contract for detecting
  `gpu-mcp.toml` mutation through file snapshot checks.
- `test/test_codex_repo_local_mcp.py`: two local fixture repos, repo-local MCP
  config selection, distinct policies, and opt-in live `codex exec` probes
  through the test proxy.
- `test/test_codex_policy_bootstrap.py`: opt-in live `codex exec` proof that a
  fresh unapproved repo can preview its first policy and that operational calls
  are refused by the real server-side quarantine.
- `test/support/`: test-only proxy server, deterministic kill-policy helper,
  unit coverage for the proxy, and experiment notes. See
  `test/support/README.md`.
- `test/test_real_gpu_mcp_battlefield.py`: opt-in wet acceptance coverage for
  cross-repo policy separation, host allowlists, staged remote execution, GPU
  framework use, argument literal handling, script and write-root escapes,
  explicit `/tmp` roots, SQLite and symlink rejection, async output policy,
  remote-control API blocks, timeout boundaries, protected policy mutation,
  raw remote-command rejection, and safe kill behavior.

## ADR Coverage

The active ADRs are source context, not a separate test backlog. ADR 0001 maps
to the broad config/bootstrap/doctor/repo-local/battlefield shape. ADR 0002 is
covered by policy reload, policy hook, doctor, and protected policy-mutation
contracts; live interactive hook approval remains a manual acceptance check
because automated tests cannot represent human judgment. ADR 0003 is covered
by staged-runner server contracts and real battlefield acceptance. Older
AI-native planning catalogs have been moved to `STALE/` and are no longer the
authoritative map of current tests.
