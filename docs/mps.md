# Managed MPS sweeps

MPS lets independent CUDA processes share one reserved GPU. GPU MCP starts the
declared runs, limits how many run together, and keeps their logs and outcomes
separate. A normal Python failure affects that run; healthy siblings continue.
An isolated CUDA initialization failure also fails only that case after its
unused context is cleared. Shared service/GPU faults are handled separately.
This works with CUDA libraries such as OpenMM and PyTorch. Scripts still cannot
start subprocesses.

## Launch

Select an available GPU with `check_gpus`, then call:

```python
run_python_on_gpu(
    host="your-approved-host", gpu_index=0, mps=True, max_concurrent=4,
    job_role="one_off", cadence_hint_sec=300,
    job_title="Temperature and chain-length sweep",
    runs=[
        {"case_id": "cool", "script_path": "jobs/polymer.py",
         "args": ["--temperature", "300", "--chain-length", "100"],
         "output_file": "logs/cool.log", "job_title": "300 K, 100 monomers"},
        {"case_id": "warm", "script_path": "jobs/polymer.py",
         "args": ["--temperature", "350", "--chain-length", "200"],
         "output_file": "logs/warm.log", "job_title": "350 K, 200 monomers"},
    ],
)
```

Paths use the existing approved script, write and log roots. Case logs must be
distinct and new. Arguments are passed directly to Python, without a shell.
The parent gets its own log and managed job ID. Case IDs may be omitted; the
server assigns stable `case-0001`, `case-0002`, etc. One request accepts up to
1,000 cases and a concurrency limit from 1 to 32. Actual CUDA context and memory
limits can reduce usable concurrency.

Use a small `job_role="smoke"` sweep when appropriate. A main sweep needs
successful same-repo smoke evidence or an explicit `smoke_skip_reason`, like
an ordinary main job. Checking intervals and descriptive durations retain their
existing meanings. `script_path`, top-level `args` and `async_mode` are for
ordinary jobs; do not combine them with `mps=True`.

## Individual and parent controls

```python
manage_gpu_job(job_id=job, case_id="cool", action="status")
manage_gpu_job(job_id=job, case_id="cool", action="logs")
manage_gpu_job(job_id=job, case_id="cool", action="stop")
manage_gpu_job(job_id=job, case_id="cool", action="retry")
manage_gpu_job(job_id=job, action="stop")  # stop active cases; cancel waiting ones
```

Retries are always explicit. GPU MCP never retries a failure automatically or
requires the agent to retry. A retry checks the script again, creates a new
attempt and log, retains previous attempts, and joins the same bounded queue.
It requires the previous process and GPU work to be gone. It does not rewrite
the script or change its arguments. After the parent releases its GPU, launch
a new sweep instead. A whole-parent retry is not supported.
Logs are preserved for every attempt. Scientific output paths follow the
script and its arguments; an explicit retry may write them again. Choose a new
launch with different output arguments when separate artifacts are needed.

Default status returns show the parent and counts. Add `case_id` for one case;
use `detail="full"` for the saved record and attempt history. A parent remains
running while other cases run or wait. Its final result succeeds only if all
current cases succeed; prior failed attempts remain in history. Exit zero is
execution evidence, not a scientific acceptance criterion.

The shared Codex backend keeps scheduled checks and private owner updates.
A case failure is reported once per attempt; shared monitoring problems are
throttled. Successful individual cases do not each wake an agent. Existing
time-based `watch` reminders are available on the parent. In Ctrl+G, a selected
MPS job adds a Runs page: Up/Down selects a case, Enter opens its details, and
Left/Right reaches its logs. `c` switches between the case and parent view.
Viewing never stops or retries work.

## Process safety and requirements

The first release supports Linux MPS protocol 2, an identifiable NVIDIA driver
470 or newer, and Volta-or-newer GPUs. Protocol 3 is refused pending separate
acceptance. No root access, compute-mode change or system MPS service is needed.
GPU MCP creates a private endpoint for its reserved GPU UUID and confirms that
each CUDA client attached before scientific code starts. It refuses fallback
execution. UUID masking also keeps framework discovery consistent with MPS.

Stopping first asks NVIDIA to terminate that client's CUDA contexts, then
signals the exact process through a Linux PID handle. If either proof fails,
the reservation stays held. `kill_gpu_process` cannot bypass this for tracked
MPS processes. A shared GPU fault pauses new starts and attempts safe group
cleanup; hardware faults can affect siblings sharing the GPU.

The launching MCP instance owns lifecycle control. If its supervisor dies,
the same instance can explicitly stop the tracked orphan group. A restarted
backend can inspect or reconcile proven completion, but cannot adopt live
clients. Missing state, uncertain launch acknowledgement or surviving service
processes never justify GPU release. `finish` requires the entire group gone;
cleanup without a verified outcome does not claim scientific success.

See [ADR 0009](../ADR/0009-managed-mps-sweeps.md) and
[the test guide](../test/MPS.md) for the decision and validation boundaries.
