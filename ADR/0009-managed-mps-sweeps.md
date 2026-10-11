# ADR 0009: Managed MPS sweeps with independent run controls

## Status

Accepted and implemented on 2026-10-11. Independent reviews checked the change
against ADRs 0001–0008 and their amendments; reported defects were corrected
and covered by regression tests.

## Context

Independent CUDA simulations can share a reserved NVIDIA GPU through MPS. The
agent needs separate scripts and parameters, bounded concurrency, individual
logs and explicit stop/retry controls. An ordinary run failure must not stop
healthy siblings. A parent job still needs to own and monitor the whole sweep.

Scripts currently cannot launch subprocesses. Keep that boundary: only captured,
trusted MCP supervisor code starts the declared guarded Python runs. Do not add
a general subprocess escape, another agent coordinator or a workflow engine.

## Decision

Extend `run_python_on_gpu` with `mps=true`, `runs` and `max_concurrent`. Ordinary
single-script calls keep their behaviour. Each run declares an optional stable
`case_id`, an approved `script_path`, arguments, a distinct approved `output_file`
and an optional title. Reject mixed single-script/sweep input and unsupported
host capabilities before execution. MPS is explicit; there is no automatic
escalation from ordinary GPU execution or from CPU-only builds.

One parent owns one normal GPU reservation, one launching server instance and
one immutable Codex thread. Individual runs have separate process identities,
attempt IDs, logs and outcomes, but no separate GPU leases. All inspection and
control remain scoped to the parent owner under ADR 0008.

`manage_gpu_job(job_id=..., case_id=..., action=...)` supports status, logs, stop
and explicit retry for one run. Omitting the selector targets the parent. A
retry joins the bounded queue and never starts while its prior process or GPU
contexts remain alive. Every attempt has distinct logs and retained provenance.
Retries are never automatic or forced. A late retry after reservation release
uses a new normal reservation through a new launch; successful outputs are not
silently reused or overwritten.
An explicit retry runs the existing arguments and may write the same scientific
output paths; the distinct-log guarantee does not change application I/O.

The parent remains running while independent runs are active or waiting, even
when a run fails. Final success requires every run to succeed. Otherwise the
parent reports a completed sweep with the failure/cancellation counts and a
failed aggregate result. Neither exit zero nor a progress bar certifies
scientific correctness. The agent decides scientific follow-up under ADR 0006.

## Process and GPU boundaries

The execution-host supervisor and authoritative records live outside approved
job write roots, like ADR 0007's build authority. Capture the worker/guard code,
validate scripts and output paths, and pin each run's source hash. Repository
state is a readiness/display mirror, never process-control authority. Prepare
real state directory targets before launch and native blind sandbox use.
Shared reservation metadata carries an inspection reference, with no case
arguments or control nonce. The full captured specification stays in authority.

Use a job-owned private MPS endpoint restricted to the reserved GPU UUID. Do not
adopt an unrelated host MPS service, change GPU compute mode, require root or
quit a shared controller. The first release supports protocol 2 on Linux,
Volta-or-newer GPUs and an identifiable NVIDIA driver 470 or newer. Refuse
protocol 3 pending separate acceptance. Fail explicitly when targeted safe
termination is unavailable. Mask both service and clients by the GPU UUID,
avoiding physical-ordinal remapping and inconsistent framework discovery.

A trusted client bootstrap initializes CUDA and confirms registration with the
owned MPS server before executing scientific code. It must not silently fall
back to ordinary CUDA. Scientific code retains the existing audit/write guards.
On normal return or a Python exception, the client waits for the supervisor to
terminate its CUDA contexts before exiting. This keeps the failure local.
An isolated pre-execution CUDA failure clears the unused bootstrap context and
fails that case. Healthy siblings continue. Group cleanup requires a shared
service/GPU fault or an explicit parent stop.

For explicit stopping, identify the actual CUDA client process, not just its
wrapper. Verify its boot/start identity and connection to this parent's MPS
server; request MPS client termination and confirm success before signalling
the OS process. Require a Linux PID handle, including syscall wrappers for
older Python builds. Failure to establish either
identity or GPU teardown refuses destructive action and retains the lease.

A native crash or shared GPU/MPS fault can affect sibling clients. Pause new
starts, record the shared problem and perform safe group cleanup; do not claim
isolation merely because other CPU processes still exist. Recovery never
automatically reruns scientific work.

If the parent disappears, queued runs cannot start, but live clients or MPS
service processes may remain. Inspection and stale cleanup must prove the whole
tracked group gone. A missing parent PID alone never frees the GPU. Missing,
malformed or inaccessible control state remains unknown. Stop/retry authority
stays with the launching server; a restarted backend may inspect and recover
verified completion but cannot adopt live work.

## Monitoring and presentation

Reuse ADR 0008's backend checks, short private polling updates and durable owner
events. Default tool returns contain parent state and counts; selected run
details and logs are available on demand. Notify a run failure once per attempt
and a persistent shared-service problem with throttling. Healthy runs do not
generate individual wake-ups. Parent completion is committed with its event
before release, and acknowledged delivery is not blindly repeated.
Child failure events retain the case and case-attempt identities. A queued
retry retires obsolete unsent observations; an uncertain acknowledgement stays
inspectable. Closing the supervisor's request queue and accepting a retry use
the same lock, so an accepted request cannot disappear during finalization.
Direct stdio hooks retain early completion readiness through an advisory
`outcome.json`; MPS status reads protected authority rather than that mirror.

The human viewer adds a Runs page to the selected MPS job, with case details
and logs and a parent/case toggle. It shows counts and selectable run records. Closing
it does not control execution. Keep progress reports separate from verified
outcomes and GPU ownership. Leave Claude-specific integrations unchanged.

## Validation

- Public launch validation rejects blocked storage, unsafe scripts, bad or
  duplicate output paths, unsupported capabilities and mixed input without a
  launch or leaked reservation.
- A real supervisor with a test-only MPS driver runs guarded subprocesses:
  concurrency is bounded; one normal failure preserves siblings; retries only
  occur after explicit requests and preserve prior attempts.
- Targeted stop proves the correct client and GPU teardown before signalling.
  Failed termination, PID reuse, parent death, lost launch acknowledgement and
  malformed state retain the reservation rather than guessing.
- Session isolation includes child selectors, logs, retries and lifecycle
  requests. Notification tests cover child failure and parent completion with
  transaction failure, duplicate delivery and archived owners.
- Native Codex blind permissions and the actual prepared paths remain usable.
  Fake MPS tests are labelled; real CUDA/MPS acceptance is opt-in and separately
  records driver, host, GPU UUID and control protocol. It must verify actual
  attachment and sibling progress during stopping, not only process exits.
- Run the existing tests and have independent reviewers check ADRs 0001–0008.
  Commit maintained code/tests/docs; exclude run artifacts. Do not push.

Maintained coverage and commands are in [the MPS test guide](../test/MPS.md).
The implementation was validated with real guarded local processes and real
PyTorch/OpenMM CUDA clients on MPS protocol 2. Independent review also added
regressions for broken parent pipes, missing identity fields, replacement
bookings, obsolete case events, shutdown admission and narrow terminal views.

## Sources

- [NVIDIA client termination](https://docs.nvidia.com/deploy/mps/latest/when-to-use-mps.html#client-early-termination)
- [NVIDIA error containment](https://docs.nvidia.com/deploy/mps/latest/when-to-use-mps.html#memory-protection-and-error-containment)
- [Legacy control interface](https://docs.nvidia.com/deploy/mps/latest/mpsv2-interface.html)
- [MPS v3 control interface](https://docs.nvidia.com/deploy/mps/latest/mpsv3-interface.html)
