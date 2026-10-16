#!/usr/bin/env python3
"""Preserve existing Codex registrations by forwarding to the shared launcher.

There is no per-session server here. Both public entrypoint names use the same
launcher, ownership checks and backend monitoring.
"""
import sys

from gpu_mcp_codex_launcher import main

SHARED_ENTRYPOINT_PROTOCOL = 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"GPU MCP startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
