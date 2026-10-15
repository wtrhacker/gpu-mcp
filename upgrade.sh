#!/usr/bin/env bash
# Run after updating this installed folder. SSH leak recovery runs before migrations.
# SSH repair is automatic on every upgrade; --preview is read-only.
set -euo pipefail
gpu_mcp_upgrade_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for gpu_mcp_upgrade_python in "${GPU_MCP_UPGRADE_PYTHON:-}" "$gpu_mcp_upgrade_root/.venv/bin/python" python3 python; do
    if [[ -n "$gpu_mcp_upgrade_python" ]] && "$gpu_mcp_upgrade_python" -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$gpu_mcp_upgrade_python" -B "$gpu_mcp_upgrade_root/gpu_mcp_upgrade.py" "$@"
    fi
done
printf '%s\n' 'GPU MCP upgrade needs Python 3.11+. Set GPU_MCP_UPGRADE_PYTHON to its path.' >&2
exit 1
