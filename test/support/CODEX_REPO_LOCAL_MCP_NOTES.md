# Codex Repo-Local MCP Probe Notes

Date: 2026-05-21

This note records what was learned from a small two-repo Codex experiment. It is
not the final GPU MCP design; it is evidence about how Codex currently behaves
with repo-local MCP configuration.

## Question

Can Codex load a different MCP server registration depending on which research
repo it is opened in, without putting repo-specific paths in the global Codex
config?

This matters because the GPU MCP server must be started with:

```text
--config /absolute/path/to/repo/gpu-mcp.toml
```

The global Codex config should not contain one fixed research repo path.

## Test Setup

Two fixture repos were created under the project root:

```text
test_mcp_repos/repo_a
test_mcp_repos/repo_b
```

Each repo contains:

- its own `gpu-mcp.toml`;
- its own `.codex/config.toml`;
- `jobs/ok_job.py`;
- `results/marker.txt` written by the job.

Repo A policy:

```text
host: gpu-a
repo root: test_mcp_repos/repo_a
```

Repo B policy:

```text
host: gpu-b
repo root: test_mcp_repos/repo_b
```

Both repo-local Codex configs register the lightweight test proxy. These
historical fixture names are test-only; the production GPU MCP server name is
`gpu-cluster-mcp` in every repo, with repo-specific behavior coming only from
the `--config` path.

```toml
[mcp_servers.repo-a-gpu-probe]
command = "/home/tingran/miniconda3/bin/python"
args = [
  "/net/levsha/scratch2/tingran/github/gpu-mcp/test/support/proxy_for_codex_exec.py",
  "serve",
  "--config",
  "/net/levsha/scratch2/tingran/github/gpu-mcp/test_mcp_repos/repo_a/gpu-mcp.toml",
]
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 10

[mcp_servers.repo-a-gpu-probe.tools.run_python_on_gpu]
approval_mode = "approve"
```

Repo B uses the same shape, but points to repo B's config and uses the server
name `repo-b-gpu-probe` for proxy-test isolation only.

## Results

`codex exec -C test_mcp_repos/repo_a` loaded repo A's local MCP server and
successfully called:

```text
repo-a-gpu-probe/run_python_on_gpu(host="gpu-a", script_path="jobs/ok_job.py")
```

The tool returned `status: ok`, repo root `test_mcp_repos/repo_a`, and stdout
`repo-a-ran`.

`codex exec -C test_mcp_repos/repo_b` loaded repo B's local MCP server and
successfully called:

```text
repo-b-gpu-probe/run_python_on_gpu(host="gpu-b", script_path="jobs/ok_job.py")
```

The tool returned `status: ok`, repo root `test_mcp_repos/repo_b`, and stdout
`repo-b-ran`.

The negative probe launched Codex in repo A and asked repo A's MCP to run on
`gpu-b`. The MCP call completed with:

```json
{"error": "host not allowed: gpu-b", "status": "rejected"}
```

Saved final-message outputs:

```text
test_mcp_repos/repo_a/codex_exec_repo_local_probe_approve.txt
test_mcp_repos/repo_b/codex_exec_repo_local_probe_approve.txt
test_mcp_repos/repo_a/codex_exec_repo_a_rejects_gpu_b.txt
```

## Important Behavior

`codex exec -C <repo>` did load and use `.codex/config.toml` from that repo.

`codex mcp list -C <repo>` did not show the repo-local MCP server in this
environment. It only showed the existing global MCP registration.

Therefore, `codex mcp list` is not a sufficient doctor check for repo-local MCP
loading. The reliable check is an actual `codex exec` probe that calls a known
MCP tool and verifies the returned repo root/config identity.

## Approval Mode

For noninteractive `codex exec`, this worked:

```toml
[mcp_servers.<server>.tools.run_python_on_gpu]
approval_mode = "approve"
```

These did not work for the probe:

- missing tool approval config;
- `approval_mode = "auto"`.

Both cases caused the MCP tool call to be cancelled in `codex exec`.

`approval_mode = "never"` is invalid in this Codex version. Valid values are:

```text
auto
prompt
approve
```

## Design Consequence

The preferred project-specific model is viable for `codex exec`:

- repo-local `.codex/config.toml` can register the GPU MCP server;
- that registration can pass the repo's own `gpu-mcp.toml` via `--config`;
- two repos can point to different policy files without storing either path in
  the global Codex config;
- the installer/doctor should validate this with `codex exec`, not with
  `codex mcp list`.

For the production install flow, the doctor should require a probe whose result
includes at least:

- MCP server name;
- config path or config hash;
- repo root;
- tool result status;
- expected host policy rejection for a repo-excluded host.
