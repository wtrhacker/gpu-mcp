# Building reusable artifacts

Build a library or exported calculation, then load it from an ordinary Python
GPU job. A local CPU-only build can use normal local tools; MCP is not required
and does not certify artifact provenance. Use `build_python_artifact` when a
build needs a remote host or GPU resources. See [ADR 0007](../ADR/0007-separate-build-and-execution.md).

| Where the build runs | How to run it |
| --- | --- |
| Local CPU | Normal local execution tools. |
| Remote CPU | `build_python_artifact(build_mode="cpu_only", ...)`. No GPU checks or reservations. |
| GPU on a configured host | `build_python_artifact(build_mode="gpu_assisted", gpu_index=..., ...)`. Normal GPU reservation rules apply. |

The requested resources and the artifact's target are separate: a CPU compiler
can produce CUDA code. Some framework export/autotuning recipes actually run
on a GPU. The agent selects the mode; the tool enforces it without automatic
promotion from CPU to GPU.

## Setup and use

Enable `build_enabled = true` through the repository's normal policy approval.
Other host, script, write and log permissions keep their existing meaning.
The execution host needs Linux user namespaces, bubblewrap, `libseccomp.so.2`,
and the configured Python environment and compiler dependencies. CUDA native
builds may need a complete development toolkit, beyond runtime libraries.

For example, submit:

```python
build_python_artifact(
    host="configured-host",
    script_path="jobs/build.py",
    artifact_path="build/calculation.so",
    build_mode="cpu_only",
    output_file="logs/build.log",
)
```

The paths above must be permitted by that repository's policy. The Python
recipe receives the absolute output path in `GPU_MCP_ARTIFACT_PATH` and creates
the artifact file or package directory there. Normal compiler subprocesses,
temporary-file cleanup and atomic replacement work inside the build boundary.

The result is a managed job handle. Use `manage_gpu_job` with its explicit
`job_id` for status, stop, retry, finish or cadence changes. Successful completion
requires exit code zero, terminated build helpers and an artifact at the
declared permitted path. Logs, the recipe path/arguments and artifact metadata
remain available. Build success does not establish numerical correctness or
portability across environments.

## Boundary and compatibility

The host filesystem is read-only except for the policy's write roots. Runtime
directories and devices are private; GPU builds expose the selected device.
Build helpers share the restriction and terminate with the build namespace.
Socket connections are blocked, including host Unix sockets; pipes, shared
memory and socketpairs work. A compiler using named-socket worker pools may
need its supported in-process compilation mode.

Later GPU execution uses the existing runner and native-package trust model.
There is no artifact registry or mandatory MCP build receipt. A loader that
still starts compilers has not completed this separation and may be rejected
by the ordinary runner's subprocess restrictions.

Acceptance coverage includes CPU-built C/CUDA extensions and a Torch 2.8
AOTInductor CUDA package loaded by the ordinary guarded GPU runner. The Torch
recipe uses GPU-assisted export and compilation. CPU-only Torch CUDA export,
JAX exported packages and arbitrary compiler worker configurations are not
established by these tests. The [test guide](../test/README.md) gives the
contained, opt-in commands.

Keep individual acceptance reports and generated artifacts local; commit the
reusable test recipes and instructions.
