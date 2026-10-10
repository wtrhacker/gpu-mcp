# ADR 0007: Separate Compilation from GPU Execution

## Status

Accepted and implemented, 2026-10-01. Local contracts and real build/load
acceptance passed, including a fresh-server GPT-5.5-medium Torch test.
See [build usage and compatibility](../docs/builds.md) and the
[acceptance test instructions](../test/README.md#commands).

## Context

GPU MCP runs approved Python jobs under static checks and runtime guards.
These jobs can load installed native libraries, but the runner's blanket
subprocess restrictions also block compiler processes used by some scientific
libraries. Building an artifact and executing it have different resource and
permission requirements.

Compilation does not necessarily require a GPU. CPU-only compilers can produce
CUDA artifacts, while some export and autotuning pipelines allocate GPU memory
or execute kernels. Neither the target architecture nor the label
"ahead-of-time" determines the resources used during a build.

Expanding the ordinary runner's permissions for every job would conflate these
two operations. A separate build capability can provide the necessary compiler
access while preserving the existing execution interface.

## Decision

Add `build_python_artifact` for building reusable artifacts. Continue to execute
applications that load those artifacts through `run_python_on_gpu`.

Local CPU-only compilation can use ordinary local tools without MCP. The build
tool supplies managed remote CPU and GPU-assisted compilation; it is not a
mandatory artifact gateway. GPU execution accepts artifacts from either route.

```text
build_python_artifact -> compiled artifact
                               |
run_python_on_gpu -> application loads and executes artifact
```

The agent chooses the requested build resource mode. The server enforces it;
it does not infer resource requirements from arbitrary Python source.

| Build mode | Resource contract |
|---|---|
| `cpu_only` (default) | No GPU access, availability queries, or reservations. The artifact may target a GPU. |
| `gpu_assisted` | Validate and reserve the selected GPU before the build can access it. |

A CPU-only build must not silently acquire GPU access. If its recipe requires
GPU computation, it fails and the agent may submit a new request in the other
mode. A library may probe for a missing device and continue without it; the
guarantee is denial of GPU access, not failure on every device probe. Retries
retain the requested mode.

## Tool Contract

The build tool accepts a configured host, an approved Python build script and
arguments, a declared artifact path, and the build mode. `gpu_index` is required
for `gpu_assisted` and rejected for `cpu_only`. The compilation target is a
separate property of the build recipe.

The tool uses the active repository policy for host, script, write, and output
permissions. It introduces no repository layout requirement. Build directories
and incremental outputs may be reused wherever that policy permits.

Add `build_enabled`, default false, to the existing policy approval and reload
mechanism. Include it in parsing, approval summaries/diffs, and reload previews.
There is no separate approval protocol or caller-supplied sandbox argument list.

Return a managed job handle with the existing logging and timing conventions.
Builds do not introduce another smoke-before-main workflow. Declare one artifact
path, which may be a regular file or a package directory.

Build success requires zero exit status, termination of the build process tree,
and the declared artifact at a permitted path. The supervisor verifies the
artifact after execution has finished and records its path, type, size where
applicable, and modification time. Recipes can retain target/toolchain metadata
alongside their outputs. These records do not certify
correctness, portability, or complete dependency reproducibility. Building does
not automatically install or execute the artifact.

## Execution Boundary

Run the build worker inside bubblewrap, using captured MCP-owned code sent
through the existing transport. Like the [staged runner model](0003-standalone-remote-safe-runner.md),
this requires no worker access to the control-side installation. This boundary
applies to the new build capability. Ordinary Python GPU jobs retain their
current runner and guards.

- Retain Python entrypoint/path validation and checks for explicit unsafe
  behavior. Permit compiler processes and their normal file operations in the
  build worker within the approved policy. Python checks remain mistake guards;
  they do not enforce native-code restrictions by themselves.
- Derive filesystem access from the configured write roots. Expose the host
  filesystem read-only, with private runtime directories and writable mounts
  for the configured roots. Dependencies need no library-specific allowlist.
  Validate path resolution and mount setup against escapes and replacement
  races. The build capability must not silently broaden or narrow those roots.
- Keep authoritative build ownership, process identity, and outcomes outside
  worker-writable paths. Repository records may be display mirrors, not
  lifecycle authority. Pin the verified worker code before granting build
  access, rather than trusting later reads from mutable staging paths.
- Use private process and network namespaces. Pipes, shared memory and
  socketpairs support build helpers. Block socket connections, including host
  pathname sockets; named-socket compiler pools are not currently supported.
  Exclude inherited descriptors that would bypass the boundary.
- In CPU-only mode, deny GPU device access to the worker and descendants,
  including access through device nodes, inherited handles, or service sockets.
  `CUDA_VISIBLE_DEVICES` alone is insufficient. GPU-assisted mode exposes the
  reserved device and required runtime support; verify physical-device mapping.
- Refuse launch if the boundary cannot be established. Do not fall back to an
  unrestricted build or enable a relaxed guard through a job-controlled flag.

Use the execution host's configured Python/compiler environment. Staging the
runner does not make control-host dependencies available remotely. Verify the
required environment on the selected host; unsupported combinations fail with
an actionable error.

This extends the trusted-lab model in [ADR 0001](0001-ai-native-gpu-mcp-design.md).
Native tools can modify or remove files through approved writable paths; Python
delete checks do not provide a native no-delete guarantee there. Path isolation
also does not create independent copies of pre-existing hardlinked data.
Build confinement does not certify the resulting native code: later artifact
execution follows the ordinary runner's existing package trust model.

## Lifecycle Integration

Reuse the managed supervisor, attempt identity, logs, outcome handling, and
`manage_gpu_job` interface. Persist job kind and resource mode in authoritative
build records. Keep the supervisor outside the build boundary, with a protected
outcome location or channel available on the execution host.

For build jobs, completion must account for the entire namespace, including
helpers that detach or change process groups. The current direct-child wait
and process-group signaling are insufficient on their own. Namespace teardown
must also cover supervisor death. Success, retry, and GPU reservation release
require confirmation that all build descendants are gone; uncertainty remains
explicit.

CPU builds have no GPU reservation key, GPU index, or GPU heartbeat. Manage them
by explicit `job_id`, using independent ownership and process-identity checks.
Status, stop, retry, finish, and cadence handling must not consult GPU usage,
reservation membership, or GPU heartbeat health. Preserve read-only status
after server restart and current-owner requirements for mutations.

GPU-assisted builds retain existing reservation and heartbeat enforcement.
Adapt reservation-oriented job records and hooks for CPU build handles without
changing existing GPU-job checks or implicit GPU-reservation targeting. Keep
outcome reconciliation in `status`, following [ADR 0005](0005-automatic-outcome-reconciliation.md),
and notification behavior consistent with [ADR 0006](0006-agent-guidance-boundaries.md).

## Implementation Plan

1. **Validate the artifact workflow.** Build a native CUDA extension without
   GPU access and load it through the existing GPU runner. Separately test a
   tensor-framework AOT package, recording whether construction requires a GPU
   and whether loading still invokes prohibited compilation operations.
   Advertise only demonstrated compiler/framework combinations.
2. **Implement the build worker.** Add a small MCP-owned staged module for the
   bubblewrap boundary and build-specific guard behavior. Enforce resource
   mode and existing filesystem policy; keep authoritative state and worker
   bootstrap separate from job-writable paths.
3. **Integrate policy and management.** Extend `gpu_mcp_config.py`, approval
   summaries, and `gpu_mcp_server.py` for the build capability. Update job/outcome
   records and hooks for genuine reservation-free CPU management. Extract shared
   lifecycle operations without weakening the existing GPU path.
4. **Verify the installed tool.** Add focused contracts and real MCP acceptance,
   then document supported build/load combinations and remaining limitations.

## Verification

Required acceptance evidence:

- CPU-only builds produce GPU-target artifacts without GPU queries,
  reservations, device access, or GPU memory allocation. Required GPU work is
  denied without automatic promotion.
- GPU-assisted builds refuse unavailable/reserved GPUs and follow existing
  reservation and ownership rules.
- Multiple repository policies produce their respective filesystem access;
  no fixed directory layout or permission scope is imposed. Test permitted
  writes and native-child denial of out-of-root paths, including symlink,
  descriptor, device, and host-socket cases within the stated trust model.
- Modified repository mirrors or staged files cannot forge authoritative job
  ownership, outcomes, or the worker code used for launch/retry.
- Completion, compiler failure, stop, supervisor death, and retry leave no
  unaccounted helpers. Include detached children and CPU lifecycle operations
  while the GPU heartbeat manager is unhealthy.
- Missing or escaping artifacts fail despite exit status zero. Loading tests
  establish numerical behavior, supported inputs, and compatibility; artifact
  identity alone is not validation.
- A worker can build when the control-side MCP installation is not visible
  there. The resulting artifact executes through a real `run_python_on_gpu`
  call in the configured application environment.
- Existing policy, guard, lifecycle, and GPU execution tests continue to pass.

Record build and execution costs separately. No new scheduler, resource-quota
system, package registry, or cache-management subsystem is part of this ADR.

## Alternatives

- **Local builds:** remain appropriate when the control host already has a
  suitable toolchain. The new capability is not mandatory for every build.
- **Relax the ordinary runner globally:** rejected because compilation-specific
  permissions would affect unrelated application jobs.
- **Sandbox every existing application job:** outside this decision; the build
  capability can be introduced independently.
- **Classify all AOT compilation as CPU-only:** rejected because framework
  preparation and autotuning can require GPU execution.

## References

- [NVRTC system requirements](https://docs.nvidia.com/cuda/archive/12.9.0/nvrtc/index.html#system-requirements): CUDA compilation without a GPU.
- [PyTorch AOTInductor](https://docs.pytorch.org/docs/2.8/torch.compiler_aot_inductor.html): artifact compilation and loading, including a GPU-assisted example.
- [Bubblewrap security model](https://github.com/containers/bubblewrap#sandbox-security): enforcement policy is the caller's responsibility.
