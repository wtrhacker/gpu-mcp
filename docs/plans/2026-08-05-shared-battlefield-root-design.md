# Shared Battlefield Root Design

## Context

GPU MCP has two relevant copies on the control machine:

- `/home/tingran/gpu-mcp` is the installed control-side runtime.
- `/net/levsha/scratch2/tingran/github/gpu-mcp` is the canonical Git checkout on
  the filesystem shared with GPU hosts.

The real battlefield suite may be launched from either copy. Its generated
fixture repos must always live on a shared filesystem so the control process and
remote GPU host see the same scripts, policies, runners, and results.

The previous default,
`/net/levsha/scratch2/tingran/gpu-mcp-battlefield-pytest`, was shared but lived
outside the narrowly authorized Git checkout. A Codex session rooted at
`/home/tingran` could therefore read it but not create the fixture.

## Decision

Use one explicit, test-owned shared fixture path regardless of which code copy
launches the suite:

```text
/net/levsha/scratch2/tingran/github/gpu-mcp/gpu-mcp-battlefield-pytest
```

`REPO_ROOT` continues to mean the code copy containing the executing test file.
It remains appropriate for locating that copy's server and support files, but it
must not determine the battlefield fixture path because it resolves differently
for the installed and canonical copies.

`GPU_MCP_BATTLEFIELD_ROOT` remains an explicit override for other shared
layouts. Existing destructive-path checks remain in force.

The generated directory is ignored by Git. Documentation must show that the
suite can be launched from either copy while using the same shared fixture.

## Permission Boundary

Future workspace-write Codex sessions may write only this additional `/net`
root:

```text
/net/levsha/scratch2/tingran/github/gpu-mcp
```

No broader `/net` write permission is required. Network access remains disabled
by default and must be approved separately for the live SSH battlefield run.

The battlefield fixture may delete and recreate only its dedicated
`gpu-mcp-battlefield-pytest` child directory. It must never target the Git
checkout itself or another human-owned directory.

## Data Flow

1. Pytest loads code from either the installed or canonical GPU MCP copy.
2. The suite creates `repo_a`, `repo_b`, policies, jobs, and results under the
   explicit shared battlefield root.
3. The control-side MCP stages its safe runner into those shared fixture repos.
4. A verified GPU host confirms it can see the fixture job before live probes
   begin.
5. Codex and the MCP execute the acceptance scenarios against those shared
   fixture repos.

## Failure Behavior

- Missing local write permission fails during fixture setup with the exact
  fixture path.
- A path that is not clearly test-owned is rejected before deletion.
- A path equal to `/`, the user's home, or the executing code copy is rejected.
- A non-shared path fails the existing remote-visibility preflight before GPU
  execution.
- SSH and GPU failures remain separate from filesystem setup failures.

## Alternatives Considered

### Derive the fixture from `REPO_ROOT`

Rejected because tests launched from `/home/tingran/gpu-mcp` would create a
non-shared fixture under `/home`.

### Require `GPU_MCP_BATTLEFIELD_ROOT` on every run

Safe and portable, but rejected as the default because it is easy to omit and
recreates the setup confusion this change is intended to remove.

### Permit the old sibling directory

Rejected because it requires a broader writable `/net` path outside the
controlled Git checkout.

## Verification

Focused non-live tests will prove that:

- the default is the explicit shared path inside the canonical checkout;
- the default does not depend on `REPO_ROOT`;
- `GPU_MCP_BATTLEFIELD_ROOT` still overrides the default;
- unsafe override paths remain rejected.

The full local suite must continue to pass. After the canonical source is
synchronized to `/home/tingran/gpu-mcp`, the real battlefield suite must run
against the shared fixture root with explicit network approval.
