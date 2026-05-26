# ADR 0003: Standalone Remote Safe Runner

## Status

Accepted. Core implementation exists:

- `gpu_mcp_safe_runner.py` is a standalone stdlib runner.
- `gpu_mcp_guard.py` contains the shared guard logic used by both the server
  and the staged runner.
- `gpu_mcp_server.py` stages/verifies it under `.gpu_mcp_runner/`.
- Remote sync and async launches use the staged runner through the shared argv
  builder.
- The legacy `gpu_mcp_server.py --safe-run` path remains available for local
  execution and compatibility tests.

## Context

`run_python_on_gpu` runs a Python job on a remote GPU host over SSH. The intended
install model is:

```text
Codex and the MCP server run on the control machine.
The MCP server SSHes to GPU hosts.
GPU hosts run deterministic Python job scripts from the shared research repo.
```

The current implementation violates that model. It builds remote commands that
run:

```bash
python /path/to/gpu_mcp_server.py --safe-run /path/to/job.py
```

This worked in the original local test repo because the MCP server file lived
under a shared `/net/...` path visible to GPU hosts. It fails when the MCP server
is installed only under the control machine's home directory, for example
`~/gpu-mcp/gpu_mcp_server.py`, because remote GPU hosts cannot open that path.

The problem is not that Codex runs on the remote host. It does not. The problem
is that the remote Python safety wrapper is bundled inside the control-side MCP
server file.

## Decision

Split the control-side MCP server from the remote safe runner.

The MCP package should contain a standalone runner source file:

```text
gpu_mcp_safe_runner.py
```

The staged runner bundle must be pure Python stdlib and must not import the
control-side MCP server, config loader, policy approval code, or SSH/MCP
dependencies. It is responsible only for guarded execution of one
already-approved Python job:

- parse runner CLI arguments;
- validate the job path against supplied script roots;
- perform the static Python safety scan;
- install runtime audit hooks;
- install pre-open write guards;
- run the target job with forwarded arguments.

The runner is standalone, not tiny. The guard constants, AST scanner, audit
hook, pre-open write guards, symlink-safe script reading, and script execution
logic live in `gpu_mcp_guard.py`. The server and staged runner both use that
module so the safety policy has one source of truth.

The control-side MCP server remains responsible for:

- loading `gpu-mcp.toml`;
- enforcing host allowlists;
- resolving script, write, and output roots;
- staging/verifying the remote runner;
- building SSH commands;
- enforcing policy reload and approval lifecycle.

Remote GPU hosts must not need the full MCP server installation path.

The key rule is:

> Remote execution uses the staged runner bundle from the repo, never an
> ambient MCP install on the remote host.

If GPU hosts happen to have their own MCP checkouts installed, those installs
are ignored for job execution. The control-side server stages the runner bundle
matching its own version into the research repo, and the remote host runs that
bundle. Multiple MCP installs across machines therefore do not break the model;
they simply are not part of the remote execution path.

## V1 Staging Model

For v1, stage the standalone runner inside the research repo:

```text
$REPO/.gpu_mcp_runner/
  gpu_mcp_safe_runner.py
  gpu_mcp_guard.py
  runner_manifest.json
```

This matches the shared-filesystem lab model already required by the project:
the remote host must see the research repo in order to run the job script and
write results. If the repo is visible remotely, the staged runner is visible
remotely.

Before launching a job, the MCP server should:

1. compute the expected bundle file hashes;
2. ensure `$REPO/.gpu_mcp_runner/gpu_mcp_safe_runner.py` and
   `$REPO/.gpu_mcp_runner/gpu_mcp_guard.py` exist;
3. replace either file if missing or hash-mismatched, using symlink-safe atomic
   staging;
4. write or update a small manifest with the staged file hashes;
5. launch the remote job through the staged runner.

Runner bundle replacement must not follow symlinks. If a staged bundle path is a
symlink or has the wrong hash, the server should write a temporary file in
`.gpu_mcp_runner/` and atomically replace the path with `os.replace`. This
prevents a malicious or accidental symlink from redirecting writes outside the
repo. Replacement should be visible in logs or tool output; it must not look
like the human's manual runner edits disappeared without explanation.

The manifest should stay small and deterministic:

```json
{
  "schema_version": 1,
  "runner_path": "/abs/repo/.gpu_mcp_runner/gpu_mcp_safe_runner.py",
  "files": {
    "gpu_mcp_safe_runner.py": {"sha256": "abc123..."},
    "gpu_mcp_guard.py": {"sha256": "def456..."}
  },
  "updated_at": "2026-05-23T00:00:00Z"
}
```

The server may read the runner bundle source from the installed package/file
path and compute expected hashes at runtime. The exact packaging API is an
implementation detail, but the source used for staging must be the one shipped
with the control-side MCP installation.

The remote command should look like:

```bash
cd "$REPO" &&
env CUDA_VISIBLE_DEVICES=0 ... \
python "$REPO/.gpu_mcp_runner/gpu_mcp_safe_runner.py" \
  --job "$REPO/jobs/foo.py" \
  --repo-root "$REPO" \
  --script-roots '["$REPO/jobs"]' \
  --write-roots '["$REPO/results"]' \
  -- arg1 arg2
```

Output file policy is enforced by the control-side server before launch. The
runner only governs the target Python job's script path, runtime behavior, and
write roots.

The repo should ignore `.gpu_mcp_runner/`.

Both sync and async remote launches must use this staged runner. The existing
async path builds a background command from the same Python argv as the sync
path, so the implementation should ensure that the shared argv-builder returns
the staged-runner command for remote hosts.

Local execution may continue to use the control-side runner entry point if that
keeps implementation smaller. The hard requirement is that remote execution must
not depend on the control-side MCP server file being visible from the GPU host.
Using the staged runner locally as well is acceptable if it simplifies tests,
but it is not required by this ADR.

## Rejected Alternatives

### Inline `python -c`

Rejected for v1. It avoids a staged file, but the safe runner is not tiny. It
contains AST scanning, audit hooks, write guards, path validation, argument
parsing, and script execution. Embedding that as a shell-quoted Python string
would be hard to read, hard to debug, and fragile in logs and SSH commands.

### Upload Runner to Remote `/tmp`

Deferred. Uploading a hash-addressed runner to `/tmp` or another remote cache is
more portable for non-shared-filesystem clusters, but it adds another SSH file
transfer path, cache invalidation, permissions, and cleanup behavior. v1 already
targets shared lab filesystems, so repo staging is simpler and more coherent.

### Install Full MCP Package on Every GPU Host

Rejected. It violates the install model and creates version-management problems.
Only the control machine should need the full MCP server installation.

### Run `python job.py` Directly

Rejected. It removes the runtime guard layer: audit hooks, pre-open write
guards, dynamic import checks, and runtime write-root enforcement.

## Testing Implications

Deterministic tests should cover:

- remote command no longer references `gpu_mcp_server.py --safe-run`;
- staged runner bundle files are rewritten when missing;
- staged runner bundle files are rewritten when hash-mismatched;
- runner manifest records the expected file hashes;
- symlinked runner bundle paths are replaced without following the symlink;
- server and staged runner share `gpu_mcp_guard.py` as the guard source of
  truth;
- standalone runner rejects script-root escapes;
- standalone runner rejects write-root escapes;
- standalone runner blocks forbidden imports/calls and runtime socket/process
  APIs;
- sync and async remote launch paths both use the standalone runner.

Battlefield tests should include one installed-copy scenario:

1. repo-local `.codex/config.toml` points to a control-side MCP server path that
   is not visible to the remote GPU host;
2. the job script lives in the shared repo;
3. `run_python_on_gpu` succeeds because the remote host runs the staged
   standalone runner, not the control-side MCP server file.

This proves the intended install model:

```text
Install MCP once on the control machine.
Stage only a small safe runner into the shared research repo.
Run deterministic jobs on remote GPU hosts through that runner.
```

This scenario is implemented as
`test/test_real_gpu_mcp_battlefield.py::test_real_installed_control_side_mcp_uses_repo_staged_runner_on_remote_host`.
It is intentionally opt-in with `GPU_MCP_RUN_REAL_BATTLEFIELD_TESTS=1` because
it uses real Codex, SSH, and a verified remote GPU host.

Manual acceptance evidence from the development environment on 2026-05-25:

- repo-local `.codex/config.toml` pointed to
  `/home/tingran/gpu-mcp/gpu_mcp_server.py`;
- `blob.mit.edu` reported `/home/tingran/gpu-mcp/gpu_mcp_server.py` as
  `not_visible`;
- `codex exec` called `gpu-cluster-mcp/run_python_on_gpu` for
  `jobs/mcp_mode_probe.py`;
- the tool returned `installed mcp probe ran`;
- `blob.mit.edu` could see both staged bundle files under
  `$REPO/.gpu_mcp_runner/`.

## Consequences

This is a localized change to remote job launch, not a redesign of the whole
project. The repo-local policy model, approved-policy lifecycle, doctor,
bootstrap, raw SSH blocking, and process-kill flow remain unchanged.

The implementation must avoid drifting into a general scheduler or portable
cluster runtime. The goal is only to decouple remote guarded execution from the
control-side MCP server install path.
