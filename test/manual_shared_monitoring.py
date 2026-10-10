#!/usr/bin/env python3
"""Prepare and observe the human-run two-session experiment. Never launch jobs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gpu_mcp_reservations as reservations
from gpu_mcp_codex_daemon import DaemonClient
from gpu_mcp_codex_launcher import alive, read_info, request
from gpu_mcp_codex_state import runtime_root


def quoted(value):
    return json.dumps(str(value))


def demo_source(stage, duration, completed, total, exit_code):
    """Standalone approved job; timing/outcome constants stay hidden from agents."""
    return f'''import argparse
import json
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--alpha', type=float, required=True)
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--batch-size', type=int, required=True)
parser.add_argument('--learning-rate', type=float, required=True)
parser.add_argument('--tag', required=True)
parser.add_argument('--output-dir', required=True)
parser.add_argument('--input')
args = parser.parse_args()
parameters = {{'alpha': args.alpha, 'seed': args.seed, 'batch_size': args.batch_size,
              'learning_rate': args.learning_rate, 'tag': args.tag}}
output = Path(args.output_dir)
output.mkdir(parents=True, exist_ok=True)
stage = {stage!r}
if stage == 'main':
    if not args.input:
        raise ValueError('the main run needs its smoke output')
    smoke = json.loads(Path(args.input).read_text())
    if smoke.get('ready') is not True or smoke.get('parameters') != parameters:
        raise ValueError('smoke output does not match the requested parameters')

total = {total}
print('Run started: ' + args.tag, flush=True)
print('GPU_MCP_PROGRESS ' + json.dumps({{'current': 0, 'total': total,
      'message': 'simulated batches'}}), flush=True)
started = time.monotonic()
values = []
for step in range(1, {completed} + 1):
    deadline = started + {duration} * step / {completed}
    time.sleep(max(0, deadline - time.monotonic()))
    value = ((args.seed + step * 17) % 101) / 100 * args.alpha + args.learning_rate * args.batch_size
    values.append(value)
    checkpoint = {{'simulated': True, 'stage': stage, 'parameters': parameters,
                  'completed_steps': step, 'total_steps': total, 'last_value': value}}
    (output / (stage + '_checkpoint.json')).write_text(json.dumps(checkpoint, indent=2) + '\\n')
    print('GPU_MCP_PROGRESS ' + json.dumps({{'current': step, 'total': total,
          'message': 'simulated batches'}}), flush=True)

(output / (stage + '_samples.csv')).write_text('step,value\\n' +
    ''.join(str(i + 1) + ',' + str(value) + '\\n' for i, value in enumerate(values)))
if stage == 'smoke':
    (output / 'smoke.json').write_text(json.dumps({{'ready': True, 'simulated': True,
        'parameters': parameters, 'steps': len(values)}}, indent=2) + '\\n')
elif {exit_code} == 0:
    (output / 'summary.json').write_text(json.dumps({{'simulated': True,
        'parameters': parameters, 'completed_steps': len(values), 'total_steps': total,
        'mean': sum(values) / len(values), 'min': min(values), 'max': max(values)}}, indent=2) + '\\n')
if {exit_code}:
    print('Validation check did not pass; partial samples and checkpoint retained.', file=sys.stderr, flush=True)
else:
    print('Run completed; outputs saved.', flush=True)
raise SystemExit({exit_code})
'''


def stage_demo(repo, plan, *, durations=None):
    """Update fixture jobs/inputs only. Never approve policy or start a backend."""
    repo = Path(repo).resolve()
    reservations.ensure_repo_job_state_root(repo)
    now = datetime.now(timezone.utc)
    round_name = 'demo_' + now.strftime('%Y%m%dT%H%M%SZ') + '_' + secrets.token_hex(2)
    plan = dict(plan, round=int(plan.get('round', 0)) + 1, round_prepared_at=now.isoformat(),
                scenario='smoke_then_main', cases={})
    durations = durations or {'a': 145, 'b': 308}
    for label, alpha, seed, smoke_sec, smoke_steps, done, total, code in (
            ('a', 0.25, 11, 35, 7, 18, 30, 17), ('b', 0.75, 29, 55, 11, 42, 42, 0)):
        directory = 'results/' + round_name + '/' + label
        (repo / directory).mkdir(parents=True)
        tag = 'parameter contrast ' + label.upper() + ': seeded baseline with small learning rate'
        args = ['--alpha', str(alpha), '--seed', str(seed), '--batch-size', '64',
                '--learning-rate', '0.001', '--tag', tag, '--output-dir', directory]
        smoke_name, main_name = f'smoke_{label}.py', f'experiment_{label}.py'
        (repo / 'jobs' / smoke_name).write_text(demo_source('smoke', smoke_sec, smoke_steps, smoke_steps, 0))
        (repo / 'jobs' / main_name).write_text(demo_source('main', durations[label], done, total, code))
        smoke_output = directory + '/smoke.json'
        spec = {'purpose': 'Run a seeded parameter trial after its smoke check, then summarize the outputs.',
                'parameters': {'alpha': alpha, 'seed': seed, 'batch_size': 64, 'learning_rate': 0.001},
                'smoke': {'script': 'jobs/' + smoke_name, 'job_role': 'smoke', 'args': args,
                          'output': smoke_output, 'acceptance': 'ready is true and parameters match'},
                'main': {'script': 'jobs/' + main_name, 'job_role': 'main',
                         'prerequisite': 'the successful smoke job from this conversation',
                         'args': args + ['--input', smoke_output],
                         'outputs': [directory + '/summary.json', directory + '/main_samples.csv'],
                         'partial_output': directory + '/main_checkpoint.json'},
                'analysis_output': directory + '/analysis.md'}
        (repo / 'work' / f'task_{label}.json').write_text(json.dumps(spec, indent=2) + '\n')
        prompt = (f'Run the smoke→main workflow in work/task_{label}.json through GPU MCP; '
                  'select an available GPU for each job, give each job a descriptive title, ')
        if label == 'b':
            prompt += 'write an analysis of work/measurements.csv to the specified analysis_output while the main job runs, '
        prompt += 'and summarize the final outputs.'
        (repo / '.human-test' / f'prompt-{label}.txt').write_text(prompt + '\n')
        plan['cases'][label] = {'script_name': main_name, 'delay_sec': durations[label],
            'exit_code': code, 'result_dir': directory, 'public_spec': f'work/task_{label}.json',
            'stages': {'smoke': {'script_name': smoke_name, 'delay_sec': smoke_sec,
                                 'completed_steps': smoke_steps, 'total_steps': smoke_steps, 'exit_code': 0},
                       'main': {'script_name': main_name, 'delay_sec': durations[label],
                                'completed_steps': done, 'total_steps': total, 'exit_code': code}}}
    (repo / '.human-test/plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def prepare(repo, *, runtime, python, nodes):
    repo, runtime = Path(repo).resolve(), Path(runtime).resolve()
    if repo.exists():
        raise ValueError("choose a new fixture directory; existing experiments are not overwritten")
    nodes = list(dict.fromkeys(str(node).strip() for node in nodes))
    if not nodes or any(not node or any(c.isspace() for c in node) for node in nodes):
        raise ValueError("provide approved host names; agents select available GPUs at launch")
    for name in ("jobs", "results", "logs", "work", ".codex", ".human-test"):
        (repo / name).mkdir(parents=True, exist_ok=True)
    (repo / ".human-test").chmod(0o700)
    plan = {"schema_version": 1, "repo": str(repo), "prepared_at": datetime.now(timezone.utc).isoformat(),
            "resource_selection": "agent", "approved_hosts": nodes, "cases": {}}
    stage_demo(repo, plan, durations={'a': 140 + secrets.randbelow(41), 'b': 280 + secrets.randbelow(41)})
    for label in ('a', 'b'):
        command = ["codex", "--cd", str(repo),
                   "--model", "gpt-5.5", "-c", 'model_reasoning_effort="medium"']
        recorder = ["script", "-q", "-e", "-f", "-c", shlex.join(command), str(repo / ".human-test" / f"session-{label}.ansi")]
        (repo / ".human-test" / f"start-{label}.sh").write_text("#!/bin/sh\nexec " + shlex.join(recorder) + "\n")
    (repo / "work/measurements.csv").write_text("sample,value\n" + "".join(f"{n},{(n * 37) % 101}\n" for n in range(1000)))
    (repo / "AGENTS.md").write_text(
        "# Repository access\n\n"
        "The jobs directory contains approved MCP entrypoints. Do not read its files, "
        "infer their contents, or inspect .human-test, logs, or .gpu_mcp_state directly. "
        "Use GPU MCP for this repository's GPU work and for your own job diagnostics. "
        "Do not edit policy or permissions.\n")
    (repo / "gpu-mcp.toml").write_text(
        f"schema_version=1\nrepo_root={quoted(repo)}\nnodes={json.dumps(nodes)}\n"
        "script_roots=['jobs']\nwrite_roots=['results']\noutput_roots=['logs']\n"
        "min_free_memory_mib=0\nsync_timeout_sec=30\n")
    denied = [repo / name for name in ("jobs", ".human-test", "logs", ".gpu_mcp_state")]
    private_code_roots = list(dict.fromkeys([ROOT / 'test', runtime / 'test']))
    registration = (
        "model='gpt-5.5'\nmodel_reasoning_effort='medium'\n"
        "default_permissions='manual-blind'\napproval_policy='on-request'\n"
        "[permissions.manual-blind.filesystem]\n':root'='read'\n" +
        "".join(f"{quoted(path)}='deny'\n" for path in private_code_roots) +
        "[permissions.manual-blind.filesystem.':workspace_roots']\n'.'='write'\n" +
        "".join(f"{quoted(name.name)}='deny'\n" for name in denied) +
        "[permissions.manual-blind.network]\nenabled=false\n"
        "[mcp_servers.gpu-cluster-mcp]\n" + f"command={quoted(python)}\n" +
        f"args=[{quoted(runtime / 'gpu_mcp_codex_launcher.py')},'--config',{quoted(repo / 'gpu-mcp.toml')}]\n" +
        f"cwd={quoted(repo)}\nenabled=true\nstartup_timeout_sec=30\ntool_timeout_sec=120\n"
        "[mcp_servers.gpu-cluster-mcp.env]\n" + f"GPU_MCP_PYTHON={quoted(python)}\n")
    for name in ("run_python_on_gpu", "manage_gpu_job", "list_gpu_jobs", "check_gpus", "list_gpu_reservations",
                 "preview_policy_reload", "reject_policy_reload", "reload_policy"):
        registration += f"[mcp_servers.gpu-cluster-mcp.tools.{name}]\napproval_mode='{ 'prompt' if name == 'reload_policy' else 'approve' }'\n"
    (repo / ".codex/config.toml").write_text(registration)
    return {"repo": str(repo), "human_files": str(repo / ".human-test"), "jobs_launched": False}


def current_case_job(jobs, script_name, prepared_at):
    """Ignore previous rounds and select by launch time, not status-update order."""
    after = datetime.fromisoformat(prepared_at.replace('Z', '+00:00'))
    matches = []
    for record in jobs:
        if record.get('script_name') != script_name:
            continue
        try:
            created = datetime.fromisoformat(record['created_at'].replace('Z', '+00:00'))
            if created >= after:
                matches.append((created, record))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return max(matches, key=lambda item: item[0])[1] if matches else None


def observation_targets(plan):
    for label, expected in plan['cases'].items():
        if expected.get('stages'):
            for stage, specification in expected['stages'].items():
                yield label + '_' + stage, specification
        else:
            yield label, expected


def observe(repo, *, duration=900, interval=5):
    repo = Path(repo).resolve()
    plan = json.loads((repo / ".human-test/plan.json").read_text())
    config = repo / "gpu-mcp.toml"
    info = read_info(runtime_root(config))
    if not alive(info):
        raise ValueError("open the two sessions first; the observer never starts a server or a job")
    client = DaemonClient()
    output = repo / ".human-test/observations.jsonl"
    deadline = time.monotonic() + duration
    try:
        with output.open("a") as trace:
            while time.monotonic() < deadline:
                snapshot = request(info, "/human/snapshot", human=True, timeout=3)
                cases = {}
                for label, expected in observation_targets(plan):
                    record = current_case_job(snapshot['jobs'], expected['script_name'],
                                              plan.get('round_prepared_at') or plan['prepared_at'])
                    if record is None:
                        continue
                    heartbeat = None
                    key = record.get("reservation_key")
                    if reservations.is_reservation_key(key):
                        try:
                            metadata = reservations.read_json_file_no_follow(
                                reservations.reservation_dir(reservations.reservation_registry_root(), key) / "metadata.json")
                            if metadata.get("job_id") == record["job_id"]:
                                heartbeat = metadata.get("last_heartbeat_at")
                        except (OSError, ValueError):
                            pass
                    owner = record["origin_thread_id"]
                    try:
                        activity = client.call("thread/read", {"threadId": owner})["thread"]["status"]
                    except Exception as exc:
                        activity = {"type": "unknown", "error": str(exc)[:300]}
                    events = [e for e in snapshot["events"] if e["job_id"] == record["job_id"]]
                    cases[label] = {"job_id": record["job_id"], "origin_thread_id": owner,
                        "host": record.get("host"), "gpu_index": record.get("gpu_index"),
                        "server_instance_id": record["server_instance_id"], "job_lifecycle": record.get("job_lifecycle"),
                        "last_status_checked_at": record.get("last_status_checked_at"), "heartbeat": heartbeat,
                        "active_reservation": record.get("active_reservation"), "outcome": record.get("outcome"),
                        "activity": activity, "events": [{"event_id": e["event_id"], "kind": e["kind"],
                                                           "state": e["state"], "updated_at": e["updated_at"]} for e in events]}
                trace.write(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), "cases": cases}) + "\n")
                trace.flush()
                time.sleep(interval)
    finally:
        client.close()
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("prepare")
    create.add_argument("--repo", type=Path, required=True)
    create.add_argument("--runtime", type=Path, required=True)
    create.add_argument("--python", type=Path, default=Path(sys.executable))
    create.add_argument("--hosts", nargs='+', required=True, help="Approved policy hosts; agents choose GPUs.")
    watch = sub.add_parser("observe")
    watch.add_argument("--repo", type=Path, required=True)
    watch.add_argument("--duration", type=int, default=900)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(json.dumps(prepare(args.repo, runtime=args.runtime, python=args.python,
            nodes=args.hosts)))
    else:
        print(observe(args.repo, duration=args.duration))


if __name__ == "__main__":
    main()
