#!/usr/bin/env python3
"""Prepare an MPS task in an existing approved demo repo; never launch work."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets


def prepare(repo, *, run_id=None):
    repo = Path(repo).expanduser().resolve()
    if not (repo / 'gpu-mcp.toml').is_file() or not (repo / '.codex/config.toml').is_file():
        raise ValueError('use an existing configured demo repository')
    run_id = run_id or 'mps_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + secrets.token_hex(2)
    if not run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in run_id):
        raise ValueError('run ID must contain only letters, digits, underscores or hyphens')
    manifest = repo / 'work' / f'{run_id}.json'
    if manifest.exists() or (repo / 'results' / run_id).exists():
        raise ValueError('run already exists; prepare a new run instead of overwriting evidence')
    source = (Path(__file__).parent / 'fixtures/mps_demo_case.py').read_text()
    script = repo / 'jobs' / f'{run_id}.py'
    if script.exists():
        raise ValueError('script already exists')

    def runs(phase, cases):
        return [{'case_id': name, 'script_path': f'jobs/{run_id}.py',
                 'job_title': f'{phase.title()} CUDA {name}',
                 'args': ['--phase', phase, '--case', name, '--seconds', str(seconds),
                          '--output-dir', f'results/{run_id}/{phase}/{name}'],
                 'output_file': f'logs/{run_id}-{phase}-{name}.log'} for name, seconds in cases]

    task = {
        'purpose': 'Run independent CUDA checks in an MPS smoke sweep, then a four-case main sweep.',
        'smoke': {'mps': True, 'max_concurrent': 2, 'job_role': 'smoke',
                  'job_title': 'MPS smoke: two independent CUDA clients', 'cadence_hint_sec': 30,
                  'runs': runs('smoke', [('smoke-one', 8), ('smoke-two', 8)])},
        'main': {'mps': True, 'max_concurrent': 2, 'job_role': 'main',
                 'job_title': 'MPS demonstration: four independent CUDA checks',
                 'cadence_hint_sec': 60, 'expected_duration_sec': 360,
                 'runs': runs('main', [('baseline', 360), ('contrast', 90), ('replicate', 90), ('confirm', 120)])},
        'instructions': [
            'Select an available supported GPU through GPU MCP for each sweep.',
            'Launch main only after successful smoke, supplying its smoke_job_id.',
            'Keep healthy siblings running if a case fails. Inspect that case through GPU MCP.',
            'You may explicitly retry a recoverable failed case once, with evidence from its error. '
            'Do not retry CUDA faults, unknown outcomes or unresolved monitoring problems.',
            'Use the normal background monitoring; request extra checks only for a concrete decision.',
            'Verify each case summary has passed=true and value=64, then summarize outcomes and attempts.',
        ],
        'outputs': [f'results/{run_id}/main/{name}/summary.json'
                    for name in ('baseline', 'contrast', 'replicate', 'confirm')],
    }
    for path in (script, manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(source)
    manifest.write_text(json.dumps(task, indent=2) + '\n')
    return {'repo': str(repo), 'task': str(manifest.relative_to(repo)), 'run_id': run_id,
            'prompt': f'Run the MPS smoke→main task in work/{run_id}.json through GPU MCP; '
                      'select available GPUs, keep healthy cases running, explicitly retry a recoverable '
                      'failed case at most once after inspecting its error, and summarize the final case results.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.repo), indent=2))


if __name__ == '__main__':
    main()
