# Managed MPS Tests

## Purpose

Verify independent case controls inside one managed GPU reservation without
weakening script restrictions, session ownership or outcome handoff. Tests
exercise the public server tools and real guarded case processes; assertions
describe outcomes rather than reproducing the supervisor's loop.

## Coverage

| Test file | What it establishes |
| --- | --- |
| `test_gpu_mcp_mps.py` | Real local processes with an explicitly simulated MPS driver: failure preserves siblings, concurrency is bounded, retries are explicit, prior logs remain, targeted stops precede OS signals, unsafe termination retains the lease, parent death does not free live clients, lost launch replies do not relaunch, event commit precedes release, and foreign sessions cannot inspect or control cases. |
| `test_gpu_mcp_codex_monitor.py` | Parent and selected-case navigation, separate details/logs and read-only human requests, alongside the existing four-page and search contracts. |
| `test_gpu_mcp_codex_transport.py` | Real shared HTTP tool schemas, native caller identity, foreign case refusal, human-only case logs and unchanged read-only snapshots. |
| `test_mps_remote_acceptance.py` | Opt-in real SSH, CUDA and MPS with PyTorch and OpenMM: actual registration, failure, targeted stop, explicit retry, healthy sibling computation, queue completion and whole-group cleanup. |

Existing policy, lease, restart, build, transport and notification suites remain
regressions. Ordinary jobs keep the original tool interface. Child controls
use the same owner authorization and per-job lock as parent controls.
Review regressions additionally cover returning after supervisor death, partial
process identities, request admission during shutdown, cancelled waiting
retries, obsolete archived child alerts, child-event insert failure, replacement
bookings and long IDs in narrow terminals. Large sweeps exercise compressed
transport and one reservation. Legacy hooks see early completion without
trusting the repository's outcome mirror.

## Running the Tests

```bash
python -m pytest -q test/test_gpu_mcp_mps.py test/test_gpu_mcp_codex_monitor.py
```

These local tests need Linux process handles and ordinary subprocess access.
The simulated driver creates test-owned sleeping service processes; it never
uses CUDA, SSH or a real GPU. Failure injection deliberately refuses GPU
teardown. Fixture cleanup uses the same safe group control and verifies that
no tracked processes remain. A restricted outer process sandbox may require
additional permission; do not change the assertions to hide that restriction.

Real acceptance is separate:

```bash
GPU_MCP_RUN_MPS_REMOTE_TESTS=1 GPU_MCP_MPS_TEST_HOST=your-host \
  python -m pytest -q -s test/test_mps_remote_acceptance.py
```

The host must be reachable through GPU MCP's dedicated SSH key, see the same
research fixture path, and provide the configured Python, CUDA driver, MPS
control binary, PyTorch and OpenMM. The test selects an available GPU through
GPU MCP and uses its real shared registry; `GPU_MCP_MPS_TEST_GPU` supplies a
preferred index. Existing reservations are preserved. Test policies, captured
authority, logs and `acceptance.json` stay under ignored
`test_mcp_repos/gpu-mcp-mps-*`; reusable tests and this guide are versioned.
No Codex daemon restart or global configuration change is involved.

## Evidence and Limits

The first contracts exposed a Python build lacking the usual PID-handle
wrappers; the worker now uses the corresponding Linux syscalls on x86-64 and
AArch64. Hardware acceptance exposed an invalid MPS `help` assumption and a
PyTorch NVML/CUDA device-count mismatch. Capability checks now inspect the
binary and driver separately, and clients use the GPU UUID mask. These were
implementation failures, not reasons to weaken the tests.

Simulated teardown receipts prove ordering and ownership logic, not NVIDIA
behavior. Real acceptance must demonstrate increasing GPU progress in the
sibling after another case is stopped and retried. A passing tiny workload does
not establish isolation from fatal GPU faults, performance gains, support for
every framework or protocol 3. No fatal GPU fault is intentionally injected on
shared lab hardware. Native model behavior remains covered by the earlier
GPT-5.5-medium monitoring experiments; direct MPS acceptance does not make a
new model-behavior claim.

## Earlier Design Compatibility

| Decision | Preserved behavior |
| --- | --- |
| ADRs 0001–0003 | Approved hosts and paths, exact policy activation, captured guard, direct arguments and subprocess restrictions. |
| ADR 0004 and its amendments | One atomic physical-GPU reservation, launching-instance control, heartbeat ownership, process identity and explicit checking intervals. |
| ADR 0005 | Verified outcomes and owner events are committed before release; uncertainty never becomes success by timeout. |
| ADR 0006 | Process facts remain separate from scientific decisions; retry and follow-up remain agent choices. |
| ADR 0007 | MPS supervision does not enable compiler or user subprocess escapes; control authority stays outside job write roots. |
| ADR 0008 | Immutable session ownership, private concise updates, independent human inspection, archived/uncertain delivery rules and automatic backend checks. |
