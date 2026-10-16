#!/usr/bin/env bash
# Run after updating this installed folder. SSH leak recovery runs before migrations.
# SSH repair is automatic on every upgrade; --preview is read-only.
set -euo pipefail
gpu_mcp_upgrade_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for gpu_mcp_upgrade_file in gpu_mcp_backend.py gpu_mcp_server.py gpu_mcp_codex_launcher.py gpu_mcp_upgrade.py; do
    if [[ ! -f "$gpu_mcp_upgrade_root/$gpu_mcp_upgrade_file" ]]; then
        printf 'GPU MCP runtime is incomplete: missing %s. Synchronize the full installed folder before upgrading.\n' "$gpu_mcp_upgrade_file" >&2
        exit 1
    fi
done
if [[ -n "${GPU_MCP_UPGRADE_PYTHON:-}" ]]; then
    if ! "$GPU_MCP_UPGRADE_PYTHON" -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        printf 'GPU_MCP_UPGRADE_PYTHON must name a working Python 3.11+: %s\n' "$GPU_MCP_UPGRADE_PYTHON" >&2
        exit 1
    fi
    exec "$GPU_MCP_UPGRADE_PYTHON" -B "$gpu_mcp_upgrade_root/gpu_mcp_upgrade.py" "$@"
fi
for gpu_mcp_upgrade_python in "$gpu_mcp_upgrade_root/.venv/bin/python" python3 python; do
    if [[ -n "$gpu_mcp_upgrade_python" ]] && "$gpu_mcp_upgrade_python" -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$gpu_mcp_upgrade_python" -B "$gpu_mcp_upgrade_root/gpu_mcp_upgrade.py" "$@"
    fi
done
printf '%s\n' 'GPU MCP upgrade needs Python 3.11+. Set GPU_MCP_UPGRADE_PYTHON to its path.' >&2
exit 1
