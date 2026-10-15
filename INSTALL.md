# Install the GPU MCP Runtime

This guide is for the human or administrator who maintains the control host. It
installs one stable GPU MCP runtime and prepares dedicated SSH access.

It does **not** configure a research repository. After the runtime is ready,
give its exact path and Python executable to an AI following
[AI_native_installer/INSTALL_FOR_AI.md](AI_native_installer/INSTALL_FOR_AI.md).

## 1. Choose the runtime deliberately

Normally, install GPU MCP in a private folder in your home directory on the
computer where the AI runs:

```bash
GPU_MCP_ROOT="$HOME/gpu-mcp"
```

This directory is the control-side runtime used by every repo-local MCP
configuration. Do not put it on the shared GPU filesystem unless your site has
a specific reason to do so: the GPU machines do not need to see it. Research
repositories and their job scripts do need to be visible on all machines at the
same absolute paths.

The checkout you are currently editing is not automatically the installed
runtime. Two layouts are supported:

- **Single checkout:** the Git checkout is also the runtime.
- **Separate deployment:** development happens in one checkout and reviewed
  files are synchronized to a different runtime directory.

For a separate deployment, keep the exact source, destination, environment,
and synchronization command in `LOCAL_DEPLOYMENT.md`. This filename is ignored
by Git. Public instructions must not guess or publish those site-specific facts.

## 2. Create the Python environment

For a new single-checkout installation:

```bash
git clone https://github.com/wtrhacker/gpu-mcp.git "$GPU_MCP_ROOT"
python3 -m venv "$GPU_MCP_ROOT/.venv"
GPU_MCP_SERVER_PYTHON="$GPU_MCP_ROOT/.venv/bin/python"
"$GPU_MCP_SERVER_PYTHON" -m pip install "mcp>=1.28,<2" fabric paramiko uvicorn websocket-client
```

For an existing or separately deployed runtime, set
`GPU_MCP_SERVER_PYTHON` to the interpreter intended to launch the server and
install the runtime dependencies into that interpreter:

```bash
GPU_MCP_SERVER_PYTHON=/absolute/path/to/python
"$GPU_MCP_SERVER_PYTHON" -m pip install "mcp>=1.28,<2" fabric paramiko uvicorn websocket-client
```

The current server targets MCP Python SDK v1. Always keep the explicit
`mcp>=1.28,<2` range; SDK v2 requires a source migration and must not be
selected by a routine dependency refresh.

For development and contract tests, install the optional test dependencies:

```bash
"$GPU_MCP_SERVER_PYTHON" -m pip install pytest jsonschema
```

Verify the exact interpreter, imports, and major SDK version:

```bash
"$GPU_MCP_SERVER_PYTHON" - <<'PY'
from importlib.metadata import version

import fabric
import mcp.server.fastmcp
import paramiko

mcp_version = version("mcp")
assert int(mcp_version.split(".", 1)[0]) == 1, mcp_version
print(f"GPU MCP runtime imports OK (mcp={mcp_version})")
PY
```

If that check fails, fix this interpreter. Do not silently point research
repositories at some other checkout or Python environment.

## 3. Bootstrap dedicated SSH access

The human runs bootstrap interactively with the exact hosts they intend to make
available:

```bash
"$GPU_MCP_SERVER_PYTHON" "$GPU_MCP_ROOT/gpu_mcp_bootstrap.py" \
  --install gpu01.example.edu gpu02.example.edu
```

Bootstrap creates or reuses `~/.ssh/gpu_mcp_key`, installs only its public key
on the selected hosts, verifies non-interactive access, and writes reachability
evidence to:

```text
~/.cache/gpu-mcp/bootstrap_hosts.json
```

That inventory proves reachability; it does not authorize every listed host for
every research repository. Each repo policy still needs explicit human review.

If credentials or host trust require interaction, complete them here. An
installer agent must not invent hosts, silently accept host keys, or create SSH
authority on its own.

## 4. Decide which Python runs GPU jobs

The MCP server runs on the control host using `GPU_MCP_SERVER_PYTHON`. Launched
jobs use `GPU_MCP_PYTHON`, which must resolve at the same absolute path on the
control host and every configured GPU host.

These may be the same interpreter if that path is shared. Otherwise, record a
separate shared, GPU-capable interpreter:

```bash
GPU_JOB_PYTHON=/shared/absolute/path/to/python
```

The repo-local MCP configuration can pass it as:

```toml
[mcp_servers.gpu-cluster-mcp.env]
GPU_MCP_PYTHON = "/shared/absolute/path/to/python"
```

The job environment must also contain the CUDA-enabled libraries used by the
research code, such as JAX or PyTorch.

## 5. Hand one research repository to the installer agent

Start the agent from the intended research repository and provide facts rather
than asking it to discover the deployment relationship:

```text
GPU MCP is installed at /absolute/path/to/gpu-mcp.
Its server Python is /absolute/path/to/python.
The GPU job Python is /shared/absolute/path/to/python.
Configure this repository by following
/absolute/path/to/gpu-mcp/AI_native_installer/INSTALL_FOR_AI.md.
Do not substitute another checkout for the installed runtime.
```

The agent will create the repo-local MCP registration and policy candidate,
then ask the human to review the raw policy preview before activation.
It also configures the keyboard monitor. On Linux with tmux 3.2+, Ctrl+G opens
the monitor in existing tmux/Byobu panes immediately. Outside tmux, new shells
load the native monitor/editor setting. Users keep ordinary `codex` / `codex
resume`. See
[the monitor guide](docs/codex-monitoring.md).

## Updating the runtime

Finish active jobs, then pull or copy the new code into the installed GPU MCP
folder, keeping local state, environments and credentials. Run:

```bash
bash ~/gpu-mcp/upgrade.sh
```

Use the actual installed path if it differs. The command finds existing
registrations for that installation, checks their Python dependencies, migrates
older registrations, adds missing tool approval entries, installs Ctrl+G and
refreshes outdated idle GPU MCP backends. It saves configuration backups and
preserves explicit tool restrictions. Repeating a completed upgrade is safe.

The command first checks shared GPU MCP backends for the old SSH connection
leak. It discovers all known GPU MCP repositories and running controllers for
the current user, including older per-session servers and separate code copies.
Configuration selection does not limit that inventory. Separate installations
are named explicitly and retain their own code and settings.
An unresponsive backend with hundreds of sockets and threads is replaced
after verifying its user, interpreter, process start time, command and protected
state. Recovery closes the old control process's connections. Detached jobs keep
running; saved jobs, pending notifications and GPU reservations stay in place.
The replacement preserves the existing ownership boundary: it does not adopt
the old instance's stop/retry authority or renew its leases. Existing completion
recovery verifies finished jobs and releases orphaned reservations with the
usual process checks. Healthy busy backends are deferred.

SSH repair runs before optional configuration migrations. If active legacy jobs
block those migrations, a completed SSH repair is reported separately. Finish
the reported legacy work and rerun for the remaining changes. This deferral is
reported without discarding the completed repair or treating busy legacy work
as a failed SSH update. Older per-session
servers load the fixed SSH helper after the usual quit/resume.

Quit and resume existing Codex conversations once to refresh their MCP tools.
New sessions use the update automatically. Ctrl+G activates immediately in
supported tmux/Byobu panes; outside tmux, open a fresh terminal for the installed
editor setting. Continue using ordinary `codex` and `codex resume`.

Optional: `bash ~/gpu-mcp/upgrade.sh --preview` shows the plan without changes.
SSH leak recovery runs automatically on every normal upgrade.
Use `--repo /absolute/research-repo` to select a single project or one absent
from saved Codex and GPU MCP records. Other installed GPU MCP copies are left
alone. Unsupported custom registrations and unapproved policy changes are
reported for review. The full maintainer test suite is not an upgrade step.

GPU policy approval is separate from tool-call approval. An unchanged
`gpu-mcp.toml` keeps its approval. Optional artifact builds require the separately
approved `build_enabled` setting. MPS extends `run_python_on_gpu` and
`manage_gpu_job`, so it needs their refreshed schemas rather than new tool names;
its execution host also needs the [MPS requirements](docs/mps.md).
Keep `reload_policy` at `approval_mode = "prompt"`.

This process does not require restarting or pinning the shared Codex daemon.
GPU MCP needs that daemon available for private result delivery, but does not
own its installation or update channel.

The [migration guide](docs/codex-monitoring.md#migration-and-upgrades) explains
discovery, safety checks and the lower-level helpers for custom installations.

## MCP startup checklist

If the MCP tools do not appear, check these in order:

1. Open the research repo's `.codex/config.toml` and identify the configured
   `command`, server script in `args`, `--config` value, and `cwd`.
2. Verify that all four absolute paths exist on the control host.
3. Run the import/version check with the configured `command` interpreter.
4. Run `codex mcp get gpu-cluster-mcp` from the research repository and confirm
   Codex resolved the expected project-scoped entry.
5. Restart Codex after any MCP command, argument, dependency, or project-config
   change, and trust the project when prompted.

Interpret outcomes carefully:

- `No such file or directory` means the configured runtime deployment is
  missing or stale.
- `No module named mcp`, `fabric`, or `paramiko` means dependencies are missing
  from the configured server interpreter.
- An error saying `FastMCP` was renamed to `MCPServer` means MCP SDK v2 was
  installed; reinstall with the explicit `mcp>=1.28,<2` range.
- `policy_state: "bootstrap_pending"` means startup succeeded and the server is
  safely quarantined until policy preview and approval.
- A policy validation error should be repaired in `gpu-mcp.toml`; it is not a
  reason to repoint the server at another checkout.

Codex loads project-scoped `.codex/config.toml` only for trusted projects and
requires a restart to launch a changed stdio command. See the
[official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

## Maintainer verification

Run deterministic tests before deploying a change:

```bash
cd "$GPU_MCP_ROOT"
"$GPU_MCP_SERVER_PYTHON" -m pytest -q -m contract
```

Live Codex and GPU tests are opt-in because they use real clients, SSH, and GPU
processes. See [test/README.md](test/README.md) and
[test/BATTLEFIELD.md](test/BATTLEFIELD.md) before running them.
