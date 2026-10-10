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

Artifact builds have local contracts in `test/test_gpu_mcp_builds.py` and short
live Codex probes in `test/test_codex_build_battlefield.py`. The latter use
GPT-5.5 medium and launch a fresh MCP server, so no interactive restart is needed:

```bash
GPU_MCP_RUN_BUILD_BATTLEFIELD_TESTS=1 pytest -q test/test_codex_build_battlefield.py -k cpu_artifact
GPU_MCP_RUN_BUILD_BATTLEFIELD_TESTS=1 GPU_MCP_BUILD_TEST_HOST=your-host GPU_MCP_BUILD_TEST_GPU=0 python -m pytest -q test/test_codex_build_battlefield.py -k torch_artifact
```

Choose an available, approved host/GPU and run pytest with the Python environment
that has the required framework and complete CUDA development toolkit. Remote
tests use real shared GPU reservations. Fixture logs, transcripts and artifacts
remain under `test_mcp_repos/gpu-mcp-build-battlefield-*`.
`test/test_build_remote_acceptance.py` offers direct server acceptance with
`GPU_MCP_RUN_BUILD_REMOTE_TESTS=1`; it is also skipped by default.

Run live repo-local Codex probes:

```bash
GPU_MCP_RUN_CODEX_EXEC_TESTS=1 pytest -m codex_exec test/test_codex_repo_local_mcp.py
```

Shared Codex ownership and monitoring have independent local contracts plus an
opt-in GPT-5.5-medium daemon/terminal suite:

```bash
pytest -q test/test_gpu_mcp_codex_state.py test/test_gpu_mcp_codex_shared.py test/test_gpu_mcp_codex_delivery.py test/test_gpu_mcp_codex_transport.py
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 pytest -q test/test_codex_shared_monitoring.py
```

See [the shared monitoring test guide](CODEX_SHARED_MONITORING.md) for fixture
boundaries, native terminal evidence and the earlier-design compatibility audit.

Private polling and temporary verification retries have local contracts and
neutral GPT-5.5-medium acceptance:

```bash
pytest -q test/test_gpu_mcp_codex_polling.py test/test_gpu_mcp_codex_delivery.py
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 pytest -q test/test_codex_private_polling.py
```

Existing-repo upgrades and slow shared HTTP calls have focused regressions:

```bash
pytest -q test/test_gpu_mcp_codex_upgrade.py test/test_gpu_mcp_codex_concurrency.py
```

They use temporary registrations, policies and jobs. They check preserved
approvals and active GPU/CPU job refusal, and reproduce health starvation while
another request is doing slow work. They do not launch models, SSH or GPU jobs.

The `/monitor` frontend has focused local checks in
`test/test_gpu_mcp_codex_cli.py`. A separate native case verifies that a human
canary never reaches the model. The [true live manual procedure](TRUE_LIVE_SHARED_MONITORING.md)
prepares two hidden, delayed jobs for human-opened sessions. Keep run reports and
captured outputs local; do not commit them.

Run the real battlefield suite:

```bash
GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1 pytest -m "codex_exec and real_battlefield" test/test_real_gpu_mcp_battlefield.py
```

The battlefield suite also reads site state such as the bootstrap inventory,
dedicated SSH key, shared fixture root, Python path, and Codex model settings.
See the constants and environment overrides at the top of
`test/test_real_gpu_mcp_battlefield.py` before running it.

## Skipped By Default

- `test/test_codex_shared_monitoring.py` requires
  `GPU_MCP_RUN_CODEX_SHARED_TESTS=1`. It uses GPT-5.5 medium, the existing Codex
  daemon, disposable conversations and isolated CPU builds. It does not restart
  the daemon or launch GPU jobs.

- `test/test_codex_repo_local_mcp.py` has three live `codex exec` tests skipped
  unless `GPU_MCP_RUN_CODEX_EXEC_TESTS=1` is set. Its pure local config tests
  still run by default.
- `test/test_real_gpu_mcp_battlefield.py` is skipped unless
  `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1` is set. It intentionally uses real
  Codex, SSH, non-local hosts from the bootstrap inventory, and shared `/net`
  repos.

- `test/test_codex_build_battlefield.py` requires
  `GPU_MCP_RUN_BUILD_BATTLEFIELD_TESTS=1`; remote cases also require an explicit
  host/GPU. `test/test_build_remote_acceptance.py` separately requires
  `GPU_MCP_RUN_BUILD_REMOTE_TESTS=1`.

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

- `test/test_gpu_mcp_builds.py`: ADR 0007 build policy, native compilation,
  filesystem/device boundaries, artifact outputs and managed lifecycle.
- `test/test_codex_build_battlefield.py`: short GPT-5.5-medium live MCP
  build/load checks; recipes are in `test/build_jobs/`.
- `test/test_build_remote_acceptance.py`: opt-in direct server/SSH acceptance
  for framework packaging and optional application coupling.

## ADR Coverage

The active ADRs are source context, not a separate test backlog. ADR 0001 maps
to the broad config/bootstrap/doctor/repo-local/battlefield shape. ADR 0002 is
covered by policy reload, policy hook, doctor, and protected policy-mutation
contracts; live interactive hook approval remains a manual acceptance check
because automated tests cannot represent human judgment. ADR 0003 is covered
by staged-runner server contracts and real battlefield acceptance. Older
AI-native planning catalogs have been moved to `STALE/` and are no longer the
authoritative map of current tests.

ADR 0007 is covered by build contracts, the policy-reload capability gate,
existing policy-hook/Stop regressions, and opt-in build/load acceptance.
The [build guide](../docs/builds.md) describes supported workflows and limits.

## Native keyboard monitor

The keyboard entry uses ordinary Codex, with setup handled by the installer.
Run its focused checks without repeating backend acceptance:

```bash
python -m pytest -q test/test_gpu_mcp_codex_editor.py
GPU_MCP_RUN_CODEX_SHARED_TESTS=1 python -m pytest -q test/test_codex_shared_monitoring.py -k native_editor
```

See [the shared monitoring guide](CODEX_SHARED_MONITORING.md).
