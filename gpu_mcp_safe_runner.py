#!/usr/bin/env python3
from __future__ import annotations

"""Standalone entrypoint staged on GPU hosts for guarded Python jobs."""

import argparse
import json
from pathlib import Path

import gpu_mcp_guard


def _path_list(raw: str, name: str) -> list[Path]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON list") from exc
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise ValueError(f"{name} must be a JSON list of strings")
    return [Path(item).expanduser().resolve() for item in values]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--script-roots", required=True)
    parser.add_argument("--write-roots", required=True)
    parser.add_argument("job_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    job_args = list(args.job_args)
    if job_args and job_args[0] == "--":
        job_args = job_args[1:]

    return gpu_mcp_guard.run_job(
        args.job,
        job_args,
        repo_root=Path(args.repo_root).expanduser().resolve(),
        script_roots=_path_list(args.script_roots, "script-roots"),
        write_roots=_path_list(args.write_roots, "write-roots"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
