#!/usr/bin/env python3
"""proxy_for_codex_exec: a tiny MCP server used only by tests.

This file is not the GPU MCP implementation. It is a deliberately small proxy
that lets tests prove how Codex loads repo-local `.codex/config.toml` entries
and passes `--config /path/to/repo/gpu-mcp.toml` into an MCP server.

It executes local Python scripts only, never SSHs, never inspects GPUs, and
implements just enough host/script-root policy to make repo-selection bugs
observable in `codex exec` tests.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


class PolicyError(ValueError):
    """Raised when a requested MCP action violates the repo-local policy."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _as_abs_config_path(path: str | Path) -> Path:
    config = Path(path).expanduser()
    if not config.is_absolute():
        raise PolicyError("--config must be an absolute path")
    return config.resolve()


@dataclass(frozen=True)
class ProxyForCodexExec:
    """Minimal policy runner behind the test-only MCP tool.

    The class mirrors a small subset of the intended production contract:
    explicit config path, repo root, host allowlist, script roots, and a
    `run_python_on_gpu` tool name. The implementation is intentionally local so
    the Codex tests can run without touching real GPU hosts.
    """

    config_path: Path
    repo_root: Path
    nodes: tuple[str, ...]
    script_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    output_roots: tuple[Path, ...]
    sync_timeout_sec: int

    @classmethod
    def from_config(cls, config_path: str | Path) -> "ProxyForCodexExec":
        """Load the repo-local policy file passed by Codex MCP config."""
        config = _as_abs_config_path(config_path)
        with config.open("rb") as fh:
            raw = tomllib.load(fh)

        repo_root = Path(raw["repo_root"]).expanduser().resolve()

        def roots(name: str) -> tuple[Path, ...]:
            return tuple((repo_root / item).resolve() for item in raw.get(name, []))

        return cls(
            config_path=config,
            repo_root=repo_root,
            nodes=tuple(raw.get("nodes", [])),
            script_roots=roots("script_roots"),
            write_roots=roots("write_roots"),
            output_roots=roots("output_roots"),
            sync_timeout_sec=int(raw.get("sync_timeout_sec", 30)),
        )

    def _resolve_script(self, script_path: str | Path) -> Path:
        """Resolve a requested script against the configured repo policy."""
        candidate = Path(script_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.repo_root / candidate
        script = candidate.resolve()

        if script.suffix != ".py":
            raise PolicyError("script must be a .py file")
        if not any(_inside(script, root) for root in self.script_roots):
            raise PolicyError("script outside approved roots")
        if not script.exists():
            raise PolicyError("script does not exist")
        return script

    def run_python_on_gpu(
        self,
        host: str,
        script_path: str,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pretend to run on a GPU host while executing locally for tests.

        The method name intentionally matches the real GPU MCP tool name so
        Codex behavior is tested against the same tool-call shape.
        """
        if host not in self.nodes:
            raise PolicyError(f"host not allowed: {host}")

        script = self._resolve_script(script_path)
        env = dict(os.environ)
        env.update(
            {
                "GPU_MCP_CONFIG": str(self.config_path),
                "GPU_MCP_REPO_ROOT": str(self.repo_root),
                "GPU_MCP_WRITE_ROOTS": os.pathsep.join(str(path) for path in self.write_roots),
                "GPU_MCP_OUTPUT_ROOTS": os.pathsep.join(str(path) for path in self.output_roots),
            }
        )

        completed = subprocess.run(
            [sys.executable, str(script), *(args or [])],
            cwd=self.repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.sync_timeout_sec,
            check=False,
        )

        return {
            "status": "ok" if completed.returncode == 0 else "error",
            "host": host,
            "repo_root": str(self.repo_root),
            "script_path": str(script),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }


def run_once_for_cli(config_path: str | Path, host: str, script_path: str) -> str:
    """Run the proxy without stdio MCP, useful for deterministic pytest checks."""
    service = ProxyForCodexExec.from_config(config_path)
    try:
        result = service.run_python_on_gpu(host=host, script_path=script_path)
    except PolicyError as exc:
        result = {"status": "rejected", "error": str(exc)}
    return json.dumps(result, sort_keys=True)


def build_mcp(config_path: str | Path) -> FastMCP:
    """Build the stdio MCP server used by live `codex exec` tests."""
    service = ProxyForCodexExec.from_config(config_path)
    mcp = FastMCP("proxy-for-codex-exec")

    @mcp.tool()
    def run_python_on_gpu(host: str, script_path: str, args: list[str] | None = None) -> str:
        try:
            result = service.run_python_on_gpu(host=host, script_path=script_path, args=args)
        except PolicyError as exc:
            result = {"status": "rejected", "error": str(exc)}
        return json.dumps(result, sort_keys=True)

    return mcp


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for either stdio MCP serving or one-shot local execution."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", required=True)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("--config", required=True)
    run_once.add_argument("--host", required=True)
    run_once.add_argument("--script-path", required=True)

    args = parser.parse_args(argv)
    if args.command == "serve":
        build_mcp(args.config).run(transport="stdio")
        return 0
    if args.command == "run-once":
        print(run_once_for_cli(args.config, args.host, args.script_path))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
