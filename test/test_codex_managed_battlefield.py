from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gpu_mcp_config import load_policy
from gpu_mcp_policy_approval import approve_policy, default_store_path
import gpu_mcp_reservations as reservations


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "gpu_mcp_server.py"

pytestmark = pytest.mark.codex_exec

live_codex_exec = pytest.mark.skipif(
    os.environ.get("GPU_MCP_RUN_CODEX_EXEC_TESTS") != "1",
    reason="set GPU_MCP_RUN_CODEX_EXEC_TESTS=1 to run live codex exec battlefield probes",
)
future_phase7_codex_api = pytest.mark.skip(
    reason="future Phase 7 API acceptance; Phase 6 live baseline tests use existing API only"
)


@dataclass(frozen=True)
class BattlefieldRepo:
    name: str
    root: Path
    server_name: str


@dataclass(frozen=True)
class ManagedBattlefield:
    root: Path
    registry: Path
    fake_bin: Path
    control_file: Path
    repo_a: BattlefieldRepo
    repo_b: BattlefieldRepo


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _write_fake_bin(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    real_ps = shutil.which("ps") or "/bin/ps"
    _write_executable(
        path / "nvidia-smi",
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "args = ' '.join(sys.argv[1:])",
                "if '--query-gpu=index,name,utilization.gpu,memory.used,memory.total' in args:",
                "    for index in range(8):",
                "        print(f'{index}, NVIDIA RTX 4090, 0, 1, 24576')",
                "elif '--query-gpu=index,uuid' in args:",
                "    for index in range(8):",
                "        print(f'{index}, GPU-fake-{index}')",
                "elif '--query-compute-apps' in args:",
                "    pass",
                "",
            ]
        ),
    )
    _write_executable(
        path / "ps",
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import subprocess",
                "import sys",
                f"real_ps = {real_ps!r}",
                "completed = subprocess.run([real_ps, *sys.argv[1:]], text=True, capture_output=True)",
                "if completed.returncode != 0 and '-p' in sys.argv:",
                "    sys.exit(0)",
                "sys.stdout.write(completed.stdout)",
                "sys.stderr.write(completed.stderr)",
                "sys.exit(completed.returncode)",
                "",
            ]
        ),
    )


def _write_jobs(repo: Path) -> None:
    jobs = repo / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    notes = repo / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (repo / "results").mkdir(parents=True, exist_ok=True)
    (repo / ".gpu_mcp_logs").mkdir(parents=True, exist_ok=True)
    (notes / "background_review.txt").write_text(
        "\n".join(
            [
                "Experiment note for the waiting-time test.",
                "Confirm that the config uses the tiny demo dataset.",
                "Confirm that the output folder is results/background_review_summary.txt.",
                "Mention that this note was reviewed while the GPU job was still running.",
                "",
            ]
        )
    )
    (jobs / "hold_gpu.py").write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import signal",
                "import sys",
                "import time",
                "from pathlib import Path",
                "",
                "repo = Path(__file__).resolve().parents[1]",
                "(repo / 'results' / 'hold_ready.txt').write_text(str(os.getpid()))",
                "control = os.environ.get('GPU_MCP_TEST_CONTROL_FILE')",
                "if '--mark-heartbeat-unhealthy' in sys.argv and control:",
                "    Path(control).write_text(json.dumps({'heartbeat_unhealthy_reason': 'fixture requested failure', 'heartbeat_unhealthy_remaining': 2}))",
                "running = True",
                "def stop(signum, frame):",
                "    global running",
                "    running = False",
                "signal.signal(signal.SIGTERM, stop)",
                "while running:",
                "    time.sleep(0.1)",
                "",
            ]
        )
    )
    (jobs / "term_delay.py").write_text(
        "\n".join(
            [
                "import signal",
                "import time",
                "from pathlib import Path",
                "",
                "repo = Path(__file__).resolve().parents[1]",
                "(repo / 'results' / 'term_ready.txt').write_text('ready')",
                "running = True",
                "def stop(signum, frame):",
                "    global running",
                "    running = False",
                "signal.signal(signal.SIGTERM, stop)",
                "while running:",
                "    time.sleep(0.1)",
                "time.sleep(1.0)",
                "",
            ]
        )
    )
    (jobs / "quick_success.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "repo = Path(__file__).resolve().parents[1]",
                "(repo / 'results' / 'success_marker.txt').write_text('quick-success\\n')",
                "print('quick-success')",
                "",
            ]
        )
    )
    (jobs / "quick_fail.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "repo = Path(__file__).resolve().parents[1]",
                "(repo / 'results' / 'fail_marker.txt').write_text('quick-fail\\n')",
                "print('quick-fail')",
                "sys.exit(7)",
                "",
            ]
        )
    )
    (jobs / "phase7_train.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import signal",
                "import time",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--mode', choices=['smoke', 'main'], required=True)",
                "parser.add_argument('--tag', default='default')",
                "args = parser.parse_args()",
                "repo = Path(__file__).resolve().parents[1]",
                "if args.mode == 'smoke':",
                "    (repo / 'results' / f'phase7_smoke_{args.tag}.txt').write_text('smoke-ok\\n')",
                "    print('phase7 smoke ok')",
                "else:",
                "    (repo / 'results' / f'phase7_main_{args.tag}.txt').write_text('main-started\\n')",
                "    running = True",
                "    def stop(signum, frame):",
                "        global running",
                "        running = False",
                "    signal.signal(signal.SIGTERM, stop)",
                "    while running:",
                "        time.sleep(0.1)",
                "",
            ]
        )
    )
    (jobs / "slow_result.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "import time",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--tag', required=True)",
                "parser.add_argument('--steps', type=int, default=24)",
                "parser.add_argument('--delay', type=float, default=1.0)",
                "args = parser.parse_args()",
                "repo = Path(__file__).resolve().parents[1]",
                "results = repo / 'results'",
                "(results / f'slow_started_{args.tag}.txt').write_text('started\\n')",
                "for step in range(args.steps):",
                "    (results / f'slow_progress_{args.tag}.txt').write_text(f'{step + 1}/{args.steps}\\n')",
                "    time.sleep(args.delay)",
                "payload = {'tag': args.tag, 'steps': args.steps, 'metric': args.steps * 7}",
                "(results / f'slow_final_{args.tag}.json').write_text(json.dumps(payload, sort_keys=True) + '\\n')",
                "print(json.dumps(payload, sort_keys=True))",
                "",
            ]
        )
    )
    (jobs / "train_with_smoke.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "import time",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--mode', choices=['smoke', 'main'], required=True)",
                "parser.add_argument('--tag', required=True)",
                "parser.add_argument('--steps', type=int, default=20)",
                "parser.add_argument('--delay', type=float, default=1.0)",
                "args = parser.parse_args()",
                "repo = Path(__file__).resolve().parents[1]",
                "results = repo / 'results'",
                "if args.mode == 'smoke':",
                "    payload = {'tag': args.tag, 'mode': 'smoke', 'metric': 1}",
                "    (results / f'train_smoke_{args.tag}.json').write_text(json.dumps(payload, sort_keys=True) + '\\n')",
                "    print(json.dumps(payload, sort_keys=True))",
                "else:",
                "    (results / f'train_main_started_{args.tag}.txt').write_text('started\\n')",
                "    for step in range(args.steps):",
                "        (results / f'train_main_progress_{args.tag}.txt').write_text(f'{step + 1}/{args.steps}\\n')",
                "        time.sleep(args.delay)",
                "    payload = {'tag': args.tag, 'mode': 'main', 'metric': args.steps * 11}",
                "    (results / f'train_main_final_{args.tag}.json').write_text(json.dumps(payload, sort_keys=True) + '\\n')",
                "    print(json.dumps(payload, sort_keys=True))",
                "",
            ]
        )
    )


def _write_repo(
    root: Path,
    name: str,
    *,
    registry: Path,
    fake_bin: Path,
    control_file: Path,
) -> BattlefieldRepo:
    repo = root / name
    server_name = f"{name.replace('-', '_')}_managed_gpu"
    (repo / ".codex").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _write_jobs(repo)
    (repo / "gpu-mcp.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                "nodes = ['localhost']",
                "script_roots = ['jobs']",
                "write_roots = ['results']",
                "output_roots = ['.gpu_mcp_logs']",
                "allowed_gpu_names = ['NVIDIA RTX 4090']",
                "min_free_memory_mib = 0",
                "sync_timeout_sec = 5",
                "",
            ]
        )
    )
    policy_store = repo / ".gpu_mcp_state" / "approved-policies.json"
    policy = load_policy(repo / "gpu-mcp.toml")
    approve_policy(
        policy,
        store_path=policy_store,
        diff_summary=["managed battlefield fixture approval"],
        approved_by="pytest",
    )
    tool_names = [
        "check_gpus",
        "kill_gpu_process",
        "manage_gpu_job",
        "list_gpu_reservations",
        "run_python_on_gpu",
    ]
    config_lines = [
        "[features]",
        "hooks = true",
        "",
        f"[mcp_servers.{server_name}]",
        f"command = {sys.executable!r}",
        "args = [",
        f"  {str(SERVER)!r},",
        '  "--config",',
        f"  {str(repo / 'gpu-mcp.toml')!r},",
        "]",
        "enabled = true",
        "startup_timeout_sec = 20",
        "tool_timeout_sec = 30",
        f"[mcp_servers.{server_name}.env]",
        'GPU_MCP_TEST_DISABLE_POLICY_APPROVAL = "1"',
        'GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS = "1"',
        f"GPU_MCP_TEST_RESERVATION_ROOT = {str(registry)!r}",
        f"GPU_MCP_TEST_CONTROL_FILE = {str(control_file)!r}",
        f"GPU_MCP_TEST_FAKE_BIN = {str(fake_bin)!r}",
        'PYTEST_CURRENT_TEST = "codex-managed-battlefield"',
        "",
    ]
    for tool in tool_names:
        config_lines.extend(
            [
                f"[mcp_servers.{server_name}.tools.{tool}]",
                'approval_mode = "approve"',
                "",
            ]
        )
    (repo / ".codex" / "config.toml").write_text("\n".join(config_lines))
    return BattlefieldRepo(name=name, root=repo, server_name=server_name)


def test_battlefield_repo_setup_does_not_write_default_policy_store(tmp_path, monkeypatch):
    approval = pytest.importorskip("gpu_mcp_policy_approval")
    default_store = tmp_path / "default-store" / "approved-policies.json"
    monkeypatch.setattr(approval, "default_store_path", lambda: default_store)
    root = tmp_path / "managed_codex_battlefield"
    registry = root / "shared_reservations"
    fake_bin = root / "fake_bin"
    control_file = root / "repo-a" / "results" / "control.json"
    _write_fake_bin(fake_bin)

    _write_repo(root, "repo-a", registry=registry, fake_bin=fake_bin, control_file=control_file)

    assert not default_store.exists()


def _remove_default_policy_approvals(policy_paths: list[Path]) -> None:
    return


def _pid_has_expected_fingerprint(pid: int, fingerprint: str) -> bool:
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return False
    except Exception:
        return False
    marker = b"GPU_MCP_PROCESS_FINGERPRINT=" + fingerprint.encode()
    return marker in environ.split(b"\0")


def _cleanup_registry_processes(registry: Path) -> None:
    if not registry.exists() or not registry.is_dir():
        return
    for metadata_path in registry.glob("*.gpu*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text())
        except Exception:
            continue
        pid = metadata.get("remote_pid")
        fingerprint = metadata.get("process_fingerprint")
        if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
            continue
        if not isinstance(fingerprint, str) or not _pid_has_expected_fingerprint(pid, fingerprint):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                break
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.05)
            if not Path(f"/proc/{pid}").exists():
                break


def _preserve_codex_exec_artifacts(root: Path) -> None:
    if os.environ.get("GPU_MCP_RUN_CODEX_EXEC_TESTS") != "1" or not root.exists():
        return
    artifact_root = REPO_ROOT / "test_mcp_repos" / "codex_exec_artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = artifact_root / f"{root.name}-{stamp}-{secrets.token_hex(4)}"
    shutil.copytree(
        root,
        dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )


@pytest.fixture()
def battlefield(tmp_path: Path) -> ManagedBattlefield:
    root = (
        REPO_ROOT
        / "test_mcp_repos"
        / f"managed_codex_battlefield_{os.getpid()}_{secrets.token_hex(4)}"
    )
    if root.exists():
        shutil.rmtree(root)
    registry = root / "shared_reservations"
    fake_bin = root / "fake_bin"
    control_file = root / "repo-a" / "results" / "control.json"
    _write_fake_bin(fake_bin)
    repo_a = _write_repo(root, "repo-a", registry=registry, fake_bin=fake_bin, control_file=control_file)
    repo_b = _write_repo(root, "repo-b", registry=registry, fake_bin=fake_bin, control_file=control_file)
    try:
        yield ManagedBattlefield(
            root=root,
            registry=registry,
            fake_bin=fake_bin,
            control_file=control_file,
            repo_a=repo_a,
            repo_b=repo_b,
        )
    finally:
        _remove_default_policy_approvals(
            [repo_a.root / "gpu-mcp.toml", repo_b.root / "gpu-mcp.toml"]
        )
        _cleanup_registry_processes(registry)
        _preserve_codex_exec_artifacts(root)
        shutil.rmtree(root, ignore_errors=True)


def _codex_env(
    battlefield: ManagedBattlefield,
    *,
    registry: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    child_env = os.environ.copy()
    child_env.update(
        {
            "GPU_MCP_TEST_DISABLE_POLICY_APPROVAL": "1",
            "GPU_MCP_TEST_ENABLE_HARNESS_CONTROLS": "1",
            "GPU_MCP_TEST_RESERVATION_ROOT": str(registry or battlefield.registry),
            "GPU_MCP_TEST_CONTROL_FILE": str(battlefield.control_file),
            "GPU_MCP_TEST_FAKE_BIN": str(battlefield.fake_bin),
            "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", "codex-managed-battlefield"),
        }
    )
    if extra:
        child_env.update(extra)
    return child_env


def _run_codex_exec(
    battlefield: ManagedBattlefield,
    repo: BattlefieldRepo,
    prompt: str,
    output_name: str,
    *,
    registry: Path | None = None,
    extra_env: dict[str, str] | None = None,
    extra_cli_args: list[str] | None = None,
    timeout: int = 180,
) -> str:
    output_path = repo.root / output_name
    child_env = _codex_env(battlefield, registry=registry, extra=extra_env)
    child_env.setdefault(
        "GPU_MCP_TEST_POLICY_APPROVAL_STORE",
        str(repo.root / ".gpu_mcp_state" / "approved-policies.json"),
    )
    try:
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "-C",
                str(repo.root),
                *(extra_cli_args or []),
                "--enable",
                "hooks",
                "--sandbox",
                "workspace-write",
                "--dangerously-bypass-hook-trust",
                "--ephemeral",
                "--skip-git-repo-check",
                "--output-last-message",
                str(output_path),
                prompt,
            ],
            cwd=REPO_ROOT,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.output or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        (output_path.with_name(output_path.name + ".stdout")).write_text(stdout)
        raise
    (output_path.with_name(output_path.name + ".stdout")).write_text(completed.stdout)
    assert completed.returncode == 0, completed.stdout
    return output_path.read_text()


def _start_codex_exec(
    battlefield: ManagedBattlefield,
    repo: BattlefieldRepo,
    prompt: str,
    output_name: str,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: int = 180,
) -> tuple[subprocess.Popen[str], Path, int]:
    output_path = repo.root / output_name
    child_env = _codex_env(battlefield, extra=extra_env)
    child_env.setdefault(
        "GPU_MCP_TEST_POLICY_APPROVAL_STORE",
        str(repo.root / ".gpu_mcp_state" / "approved-policies.json"),
    )
    proc = subprocess.Popen(
        [
            "codex",
            "exec",
            "-C",
            str(repo.root),
            "--enable",
            "hooks",
            "--sandbox",
            "workspace-write",
            "--dangerously-bypass-hook-trust",
            "--ephemeral",
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_path),
            prompt,
        ],
        cwd=REPO_ROOT,
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc, output_path, timeout


def _finish_codex_exec(started: tuple[subprocess.Popen[str], Path, int]) -> str:
    proc, output_path, timeout = started
    stdout, _ = proc.communicate(timeout=timeout)
    (output_path.with_name(output_path.name + ".stdout")).write_text(stdout)
    assert proc.returncode == 0, stdout
    return output_path.read_text()


def _json_values_from_text(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        values.append(value)
    return values


def _collect_payloads(value: Any) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "result" in value and isinstance(value["result"], str):
            for parsed in _json_values_from_text(value["result"]):
                payloads.extend(_collect_payloads(parsed))
        if "status" in value or "gpus" in value or "reservations" in value:
            payloads.append(value)
        for item in value.values():
            payloads.extend(_collect_payloads(item))
    elif isinstance(value, list):
        for item in value:
            payloads.extend(_collect_payloads(item))
    elif isinstance(value, str):
        for parsed in _json_values_from_text(value):
            payloads.extend(_collect_payloads(parsed))
    return payloads


def _tool_payloads(final_message: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for value in _json_values_from_text(final_message):
        payloads.extend(_collect_payloads(value))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        encoded = json.dumps(payload, sort_keys=True)
        if encoded in seen:
            continue
        seen.add(encoded)
        unique.append(payload)
    return unique


def _tool_payloads_in_order(final_message: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for value in _json_values_from_text(final_message):
        payloads.extend(_collect_payloads(value))
    return payloads


def _first_payload(final_message: str, **matches: Any) -> dict[str, Any]:
    for payload in _tool_payloads(final_message):
        if all(payload.get(key) == value for key, value in matches.items()):
            return payload
    raise AssertionError(f"no payload matching {matches!r} in:\n{final_message}")


def _append_phase7_trace_event(
    trace_file: Path,
    *,
    run_id: str,
    scenario: str,
    event: str,
    **fields: Any,
) -> None:
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    event_index = 0
    if trace_file.exists():
        event_index = sum(1 for _ in trace_file.open())
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario": scenario,
        "event_index": event_index,
        "event": event,
        **fields,
    }
    with trace_file.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def _phase7_trace_events(trace_file: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in trace_file.read_text().splitlines()
        if line.strip()
    ]


def _phase7_tool_calls(
    events: list[dict[str, Any]],
    *,
    tool: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    calls = [event for event in events if event.get("event") == "tool_call"]
    if tool is not None:
        calls = [event for event in calls if event.get("tool") == tool]
    if action is not None:
        calls = [event for event in calls if event.get("args", {}).get("action") == action]
    return calls


def _phase7_tool_results(
    events: list[dict[str, Any]],
    *,
    tool: str | None = None,
    status: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    results = [event for event in events if event.get("event") == "tool_result"]
    if tool is not None:
        results = [event for event in results if event.get("tool") == tool]
    if status is not None:
        results = [
            event
            for event in results
            if event.get("response_summary", {}).get("status") == status
        ]
    if action is not None:
        results = [
            event
            for event in results
            if event.get("response_summary", {}).get("action") == action
        ]
    return results


def _set_repo_mcp_env(repo: BattlefieldRepo, key: str, value: str) -> None:
    config = repo.root / ".codex" / "config.toml"
    text = config.read_text()
    replacement_line = f"{key} = {value!r}"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key} = "):
            lines[index] = replacement_line
            config.write_text("\n".join(lines) + "\n")
            return
    marker = 'PYTEST_CURRENT_TEST = "codex-managed-battlefield"\n'
    replacement = marker + replacement_line + "\n"
    assert marker in text
    config.write_text(text.replace(marker, replacement, 1))


def _phase7_negative_control_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    status_calls = [
        event
        for event in events
        if event.get("event") == "tool_call"
        and event.get("tool") == "manage_gpu_job"
        and event.get("args", {}).get("action") == "status"
    ]
    launch_results = [
        event
        for event in events
        if event.get("event") == "tool_result"
        and event.get("tool") == "run_python_on_gpu"
        and event.get("response_summary", {}).get("status") == "launched"
    ]
    if len(status_calls) >= 2 and launch_results:
        return {
            "verdict": "hard_fail",
            "reason": "negative control made repeated immediate status calls after launch",
            "status_call_count": len(status_calls),
        }
    return {
        "verdict": "inconclusive",
        "reason": "trace did not contain the expected launch plus repeated status calls",
        "status_call_count": len(status_calls),
    }


def _phase7_not_due_stop_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    status_calls = [
        event
        for event in events
        if event.get("event") == "tool_call"
        and event.get("tool") == "manage_gpu_job"
        and event.get("args", {}).get("action") == "status"
    ]
    not_due_results = [
        event
        for event in events
        if event.get("event") == "tool_result"
        and event.get("tool") == "manage_gpu_job"
        and event.get("response_summary", {}).get("polling_state") == "not_due_yet"
    ]
    if len(status_calls) == 1 and len(not_due_results) == 1:
        return {
            "verdict": "pass",
            "reason": "agent stopped after one compact not_due_yet response",
            "status_call_count": len(status_calls),
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not stop cleanly after compact not_due_yet response",
        "status_call_count": len(status_calls),
        "not_due_result_count": len(not_due_results),
    }


def _phase7_early_override_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    status_calls = _phase7_tool_calls(events, tool="manage_gpu_job", action="status")
    override_calls = [
        event
        for event in status_calls
        if event.get("args", {}).get("has_early_poll_reason") is True
    ]
    full_results = [
        event
        for event in _phase7_tool_results(events, tool="manage_gpu_job", action="status")
        if event.get("response_summary", {}).get("full_status_performed") is True
    ]
    if len(status_calls) == 1 and len(override_calls) == 1 and len(full_results) == 1:
        return {
            "verdict": "pass",
            "reason": "agent used early_poll_reason for the user-forced early check",
        }
    return {
        "verdict": "hard_fail",
        "reason": "user-forced early check did not use an explicit early_poll_reason",
        "status_call_count": len(status_calls),
        "override_call_count": len(override_calls),
        "full_status_result_count": len(full_results),
    }


def _phase7_smoke_to_main_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    smoke_launches = [
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu", status="launched")
        if event.get("response_summary", {}).get("job_role") == "smoke"
    ]
    if not smoke_launches:
        return {"verdict": "hard_fail", "reason": "no smoke job was launched"}
    smoke_job_id = smoke_launches[0]["response_summary"].get("job_id")
    smoke_status_success = [
        event
        for event in _phase7_tool_results(events, tool="manage_gpu_job", status="ok")
        if event.get("response_summary", {}).get("job_id") == smoke_job_id
        and event.get("response_summary", {}).get("job_lifecycle") == "succeeded"
    ]
    main_calls = [
        event
        for event in _phase7_tool_calls(events, tool="run_python_on_gpu")
        if event.get("args", {}).get("job_role") == "main"
        and event.get("args", {}).get("smoke_job_id") == smoke_job_id
    ]
    main_launches = [
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu", status="launched")
        if event.get("response_summary", {}).get("job_role") == "main"
    ]
    if smoke_status_success and main_calls and main_launches:
        return {
            "verdict": "pass",
            "reason": "agent checked successful smoke and linked that smoke job to the main launch",
            "smoke_job_id": smoke_job_id,
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not complete the smoke-to-main chain",
        "smoke_job_id": smoke_job_id,
        "smoke_status_success_count": len(smoke_status_success),
        "main_call_count": len(main_calls),
        "main_launch_count": len(main_launches),
    }


def _phase7_missing_smoke_recovery_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    smoke_refusals = [
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu", status="refused")
        if "smoke" in str(event.get("response_summary", {}).get("reason", "")).lower()
    ]
    smoke_launches = [
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu", status="launched")
        if event.get("response_summary", {}).get("job_role") == "smoke"
    ]
    if smoke_refusals and smoke_launches:
        return {
            "verdict": "pass",
            "reason": "agent recovered from missing smoke by launching a smoke job",
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not recover from the missing-smoke guard by running smoke",
        "smoke_refusal_count": len(smoke_refusals),
        "smoke_launch_count": len(smoke_launches),
    }


def _phase7_smoke_skip_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    main_results = [
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu")
        if event.get("response_summary", {}).get("job_role") == "main"
    ]
    skip_calls = [
        event
        for event in _phase7_tool_calls(events, tool="run_python_on_gpu")
        if event.get("args", {}).get("has_smoke_skip_reason") is True
    ]
    conservative = [
        event
        for event in main_results
        if event.get("response_summary", {}).get("job_role") == "main"
        and event.get("response_summary", {}).get("heartbeat_interval_sec") == 60
    ]
    if skip_calls and conservative:
        return {
            "verdict": "pass",
            "reason": "agent skipped smoke explicitly and received conservative first check timing",
        }
    return {
        "verdict": "hard_fail",
        "reason": "smoke skip was not explicit or did not use conservative timing",
        "main_result_count": len(main_results),
        "skip_call_count": len(skip_calls),
        "conservative_launch_count": len(conservative),
    }


def _phase7_update_cadence_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    update_calls = _phase7_tool_calls(events, tool="manage_gpu_job", action="update_cadence")
    good_update_calls = [
        event
        for event in update_calls
        if event.get("args", {}).get("has_reason") is True
        and (
            event.get("args", {}).get("expected_duration_sec") is not None
            or event.get("args", {}).get("cadence_hint_sec") is not None
        )
    ]
    update_results = _phase7_tool_results(events, tool="manage_gpu_job", status="ok", action="update_cadence")
    if good_update_calls and update_results:
        return {
            "verdict": "pass",
            "reason": "agent updated the job wait time with a reason",
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not make a valid update_cadence call",
        "update_call_count": len(update_calls),
        "good_update_call_count": len(good_update_calls),
        "update_result_count": len(update_results),
    }


def _phase7_two_job_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    launched = _phase7_tool_results(events, tool="run_python_on_gpu", status="launched")
    status_calls = _phase7_tool_calls(events, tool="manage_gpu_job", action="status")
    job_ids = {
        event.get("response_summary", {}).get("job_id")
        for event in launched
        if event.get("response_summary", {}).get("job_id")
    }
    if len(job_ids) >= 2 and not status_calls:
        return {
            "verdict": "pass",
            "reason": "agent launched two jobs and did not check either before reporting next check times",
            "job_count": len(job_ids),
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not keep two running jobs separate without early status checks",
        "job_count": len(job_ids),
        "status_call_count": len(status_calls),
    }


def _phase7_due_reminder_verdict(events: list[dict[str, Any]], *, job_id: str) -> dict[str, Any]:
    status_calls = [
        event
        for event in _phase7_tool_calls(events, tool="manage_gpu_job", action="status")
        if event.get("args", {}).get("job_id") == job_id
    ]
    full_results = [
        event
        for event in _phase7_tool_results(events, tool="manage_gpu_job", status="ok")
        if event.get("response_summary", {}).get("job_id") == job_id
        and event.get("response_summary", {}).get("full_status_performed") is True
    ]
    if status_calls and full_results:
        return {
            "verdict": "pass",
            "reason": "agent checked the due job after the hook reminder",
        }
    return {
        "verdict": "hard_fail",
        "reason": "agent did not status-check the due job",
        "status_call_count": len(status_calls),
        "full_status_result_count": len(full_results),
    }


def _phase7_repo_silence_verdict(events: list[dict[str, Any]]) -> dict[str, Any]:
    status_calls = _phase7_tool_calls(events, tool="manage_gpu_job", action="status")
    if not status_calls:
        return {
            "verdict": "pass",
            "reason": "other repo did not receive or act on the first repo's job reminder",
        }
    return {
        "verdict": "hard_fail",
        "reason": "other repo made a job-specific status call when it should not have",
        "status_call_count": len(status_calls),
    }


def _first_payload_with_key(final_message: str, key: str) -> dict[str, Any]:
    for payload in _tool_payloads(final_message):
        if key in payload:
            return payload
    raise AssertionError(f"no payload with key {key!r} in:\n{final_message}")


def _payloads_with_key(final_message: str, key: str) -> list[dict[str, Any]]:
    return [payload for payload in _tool_payloads(final_message) if key in payload]


def _prompt(repo: BattlefieldRepo, instructions: str) -> str:
    return (
        "Do not run shell commands. Do not edit files. Use only MCP tools whose "
        f"name starts with mcp__{repo.server_name}__. Do not use gpu_cluster_mcp "
        f"or gpu-cluster tools. After every MCP call, include the exact parsed "
        "JSON result in your final answer as JSON. "
        + instructions
    )


def _phase7_prompt(repo: BattlefieldRepo, instructions: str) -> str:
    return (
        "You are working in this repo for a user who wants GPU work managed through "
        "the GPU MCP. You may inspect or edit normal local repo files if the task "
        "requires it. Do not use SSH, ps, nvidia-smi, /proc, or raw process "
        "inspection to bypass the MCP for GPU job state. Use only MCP tools whose name starts "
        f"with mcp__{repo.server_name}__ for GPU job launch, GPU job status, GPU "
        "reservation state, and GPU cleanup. Do not use gpu_cluster_mcp or "
        "gpu-cluster tools. In your final answer, include one JSON object with "
        "key 'mcp_results'. Its value must be a list, in order, with one object "
        "per MCP call: {'call': '<short label>', 'result': <exact parsed JSON result>}. "
        "Also include key 'final_metrics' as an object containing only the final "
        "numeric metrics you are reporting; use an empty object if no final metric "
        "is available yet. "
        + instructions
    )


def _run_phase7_codex_trace(
    battlefield: ManagedBattlefield,
    repo: BattlefieldRepo,
    *,
    trace_file: Path,
    run_id: str,
    scenario: str,
    prompt: str,
    output_name: str,
    timeout: int = 180,
) -> tuple[str, list[dict[str, Any]]]:
    _set_repo_mcp_env(repo, "GPU_MCP_TEST_PHASE7_TRACE_FILE", str(trace_file))
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="prompt",
        text=prompt,
    )
    final_message = _run_codex_exec(
        battlefield,
        repo,
        prompt,
        output_name,
        extra_env={"GPU_MCP_TEST_PHASE7_TRACE_FILE": str(trace_file)},
        timeout=timeout,
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="final_answer",
        text=final_message,
    )
    return final_message, _phase7_trace_events(trace_file)


def _append_phase7_machine_check(
    trace_file: Path,
    *,
    run_id: str,
    scenario: str,
    verdict: dict[str, Any],
) -> None:
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="machine_check",
        result=verdict,
    )


def _short_text(value: object, limit: int = 4000) -> object:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f"... [trimmed {len(value) - limit} chars]"


def _phase7_failure_report(
    *,
    expected: str,
    verdict: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    flow: list[dict[str, Any]] = []
    for event in events:
        event_type = event.get("event")
        if event_type == "prompt":
            flow.append({"event": "prompt", "text": _short_text(event.get("text"), 2000)})
        elif event_type == "tool_call":
            flow.append({
                "event": "tool_call",
                "tool": event.get("tool"),
                "args": event.get("args"),
            })
        elif event_type == "tool_result":
            flow.append({
                "event": "tool_result",
                "tool": event.get("tool"),
                "response_summary": event.get("response_summary"),
            })
        elif event_type == "final_answer":
            flow.append({"event": "final_answer", "text": _short_text(event.get("text"))})
        elif event_type == "machine_check":
            flow.append({"event": "machine_check", "result": event.get("result")})
    return json.dumps(
        {
            "expected_verdict": expected,
            "actual_verdict": verdict.get("verdict"),
            "verdict": verdict,
            "flow": flow,
        },
        indent=2,
        sort_keys=True,
    )


def _assert_phase7_verdict(
    expected: str,
    verdict: dict[str, Any],
    events: list[dict[str, Any]],
) -> None:
    assert verdict["verdict"] == expected, _phase7_failure_report(
        expected=expected,
        verdict=verdict,
        events=events,
    )


def _force_phase7_job_due(repo: BattlefieldRepo, job_id: str) -> None:
    record_path = reservations.job_record_path(repo.root, job_id)
    record = json.loads(record_path.read_text())
    record["next_poll_after"] = "2000-01-01T00:00:00Z"
    record.pop("last_status_checked_at", None)
    reservations.atomic_write_json(record_path, record)


def _phase7_baseline_trace_path(repo: BattlefieldRepo, name: str) -> Path:
    return repo.root / ".gpu_mcp_state" / f"{name}.jsonl"


def _job_record(repo: BattlefieldRepo, job_id: str) -> dict[str, Any]:
    return json.loads(reservations.job_record_path(repo.root, job_id).read_text())


def _all_job_records(repo: BattlefieldRepo) -> list[dict[str, Any]]:
    jobs_dir = repo.root / ".gpu_mcp_state" / "jobs"
    if not jobs_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("*/job.json")):
        try:
            records.append(json.loads(path.read_text()))
        except Exception:
            continue
    return records


def _phase6_launch_payloads(final_message: str) -> list[dict[str, Any]]:
    return [
        payload
        for payload in _tool_payloads_in_order(final_message)
        if payload.get("status") in {"launched", "launched_with_warning"}
        and payload.get("job_id")
        and payload.get("reservation_key")
    ]


def _phase6_status_payloads(
    final_message: str,
    *,
    job_id: str | None = None,
) -> list[dict[str, Any]]:
    payloads = [
        payload
        for payload in _tool_payloads_in_order(final_message)
        if payload.get("status") == "ok" and payload.get("action") == "status"
    ]
    if job_id is not None:
        payloads = [payload for payload in payloads if payload.get("job_id") == job_id]
    return payloads


def _mcp_result_entries(final_message: str) -> list[dict[str, Any]]:
    candidates: list[list[dict[str, Any]]] = []
    for value in _json_values_from_text(final_message):
        if not isinstance(value, dict):
            continue
        entries = value.get("mcp_results")
        if not isinstance(entries, list):
            continue
        if all(isinstance(entry, dict) for entry in entries):
            candidates.append(entries)
    if not candidates:
        return []
    return max(candidates, key=len)


def _entry_result(entry: dict[str, Any]) -> dict[str, Any]:
    result = entry.get("result")
    return result if isinstance(result, dict) else {}


def _entry_tool_name(entry: dict[str, Any]) -> str | None:
    result = _entry_result(entry)
    if result.get("status") in {"launched", "launched_with_warning", "refused"}:
        return "run_python_on_gpu"
    if result.get("action") in {"status", "stop", "retry", "finish"}:
        return "manage_gpu_job"
    if "gpus" in result:
        return "check_gpus"
    if "reservations" in result:
        return "list_gpu_reservations"
    return None


def _entry_call_text(entry: dict[str, Any]) -> str:
    return str(entry.get("call", "")).lower()


def _mcp_launch_entries(
    final_message: str,
    *,
    output_contains: str | None = None,
) -> list[dict[str, Any]]:
    entries = []
    for entry in _mcp_result_entries(final_message):
        result = _entry_result(entry)
        if result.get("status") not in {"launched", "launched_with_warning"}:
            continue
        if output_contains and output_contains not in str(result.get("output", {}).get("path", "")):
            continue
        entries.append(entry)
    return entries


def _mcp_status_entries(
    final_message: str,
    *,
    job_id: str | None = None,
    call_contains: str | None = None,
) -> list[dict[str, Any]]:
    entries = []
    for entry in _mcp_result_entries(final_message):
        result = _entry_result(entry)
        if result.get("status") != "ok" or result.get("action") != "status":
            continue
        if job_id is not None and result.get("job_id") != job_id:
            continue
        if call_contains is not None and call_contains.lower() not in _entry_call_text(entry):
            continue
        entries.append(entry)
    return entries


def _codex_stdout(repo: BattlefieldRepo, output_name: str) -> str:
    path = repo.root / f"{output_name}.stdout"
    return path.read_text() if path.exists() else ""


def _codex_stream_events(stdout_text: str, server_name: str) -> list[dict[str, Any]]:
    prefix = f"mcp: {server_name}/"
    lines = stdout_text.splitlines()
    events: list[dict[str, Any]] = []
    mcp_started_before = 0
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith(prefix) and line.endswith(" started"):
            tool = line[len(prefix):-len(" started")]
            events.append(
                {
                    "event": "mcp_started",
                    "tool": tool,
                    "line": index + 1,
                    "mcp_started_before": mcp_started_before,
                }
            )
            mcp_started_before += 1
            continue
        if line != "exec":
            continue
        command = ""
        for candidate in lines[index + 1:index + 5]:
            candidate = candidate.strip()
            if not candidate or candidate.startswith("hook:"):
                continue
            command = candidate
            break
        events.append(
            {
                "event": "shell_command",
                "command": command,
                "line": index + 1,
                "mcp_started_before": mcp_started_before,
            }
        )
    return events


def _mcp_started_sequence(stdout_text: str, server_name: str) -> list[str]:
    return [
        str(event["tool"])
        for event in _codex_stream_events(stdout_text, server_name)
        if event["event"] == "mcp_started"
    ]


def _mcp_started_counts(stdout_text: str, server_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tool in _mcp_started_sequence(stdout_text, server_name):
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def _reported_mcp_sequence(final_message: str) -> list[str]:
    return [
        tool
        for entry in _mcp_result_entries(final_message)
        for tool in [_entry_tool_name(entry)]
        if tool is not None
    ]


def _reported_mcp_counts(final_message: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tool in _reported_mcp_sequence(final_message):
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def _mcp_history_audit(
    repo: BattlefieldRepo,
    *,
    output_name: str,
    final_message: str,
) -> dict[str, Any]:
    stdout_text = _codex_stdout(repo, output_name)
    started_sequence = _mcp_started_sequence(stdout_text, repo.server_name)
    reported_sequence = _reported_mcp_sequence(final_message)
    started_counts = _mcp_started_counts(stdout_text, repo.server_name)
    reported_counts = _reported_mcp_counts(final_message)
    missing = {
        tool: count - reported_counts.get(tool, 0)
        for tool, count in started_counts.items()
        if count > reported_counts.get(tool, 0)
    }
    extra = {
        tool: count - started_counts.get(tool, 0)
        for tool, count in reported_counts.items()
        if count > started_counts.get(tool, 0)
    }
    return {
        "started_counts": started_counts,
        "reported_counts": reported_counts,
        "started_sequence": started_sequence,
        "reported_sequence": reported_sequence,
        "sequence_mismatch": started_sequence != reported_sequence,
        "missing_reported_results": missing,
        "extra_reported_results": extra,
    }


def _mcp_history_has_mismatch(audit: dict[str, Any]) -> bool:
    return bool(
        audit["sequence_mismatch"]
        or audit["missing_reported_results"]
        or audit["extra_reported_results"]
    )


def _entry_position(entries: list[dict[str, Any]], target: dict[str, Any]) -> int | None:
    for index, entry in enumerate(entries):
        if entry is target or entry == target:
            return index
    return None


def _is_broad_gpu_check_entry(entry: dict[str, Any]) -> bool:
    result = _entry_result(entry)
    return "gpus" in result or "reservations" in result


def _broad_gpu_check_count_between(
    entries: list[dict[str, Any]],
    *,
    start_index: int | None,
    end_index: int | None,
) -> int:
    if start_index is None or end_index is None:
        return 0
    return sum(
        1
        for index, entry in enumerate(entries)
        if start_index < index < end_index and _is_broad_gpu_check_entry(entry)
    )


def _shell_commands_between_mcp_entries(
    stdout_text: str,
    server_name: str,
    *,
    start_index: int | None,
    end_index: int | None,
) -> list[str]:
    if start_index is None:
        return []
    start_ordinal = start_index + 1
    end_ordinal = end_index + 1 if end_index is not None else 1_000_000_000
    return [
        str(event.get("command", ""))
        for event in _codex_stream_events(stdout_text, server_name)
        if event["event"] == "shell_command"
        and start_ordinal <= int(event["mcp_started_before"]) < end_ordinal
    ]


def _raw_gpu_shell_commands(stdout_text: str, server_name: str) -> list[str]:
    commands = [
        str(event.get("command", ""))
        for event in _codex_stream_events(stdout_text, server_name)
        if event["event"] == "shell_command"
    ]
    banned_tokens = ("nvidia-smi", "/proc", " pgrep ", " pkill ", " ps ", " ssh ")
    banned_prefixes = ("pgrep ", "pkill ", "ps ", "ssh ")
    return [
        command
        for command in commands
        for lowered in [f" {command.lower()} "]
        if any(token in lowered for token in banned_tokens)
        or command.lower().strip().startswith(banned_prefixes)
    ]


def _state_peek_shell_commands_before_terminal(
    stdout_text: str,
    server_name: str,
    *,
    launch_index: int | None,
    terminal_index: int | None,
) -> list[str]:
    commands = _shell_commands_between_mcp_entries(
        stdout_text,
        server_name,
        start_index=launch_index,
        end_index=terminal_index,
    )
    state_paths = (".gpu_mcp_logs", ".gpu_mcp_state", "results/slow_", "results/train_")
    allowed_paths = ("results/background_review_summary.txt",)
    return [
        command
        for command in commands
        for lowered in [command.lower()]
        if any(path in lowered for path in state_paths)
        and not any(path in lowered for path in allowed_paths)
    ]


def _sleep_shell_commands_while_active(
    stdout_text: str,
    server_name: str,
    *,
    launch_index: int | None,
    terminal_index: int | None,
) -> list[str]:
    commands = _shell_commands_between_mcp_entries(
        stdout_text,
        server_name,
        start_index=launch_index,
        end_index=terminal_index,
    )
    return [
        command
        for command in commands
        for lowered in [command.lower()]
        if "sleep" in lowered
    ]


def _final_metrics(final_message: str) -> dict[str, Any]:
    for value in _json_values_from_text(final_message):
        if not isinstance(value, dict):
            continue
        metrics = value.get("final_metrics")
        if isinstance(metrics, dict):
            return metrics
    return {}


def _metric_reported(final_message: str, metric: int) -> bool:
    return any(value == metric for value in _final_metrics(final_message).values())


def _active_wait_violation(
    repo: BattlefieldRepo,
    *,
    output_name: str,
    launch_index: int | None,
    terminal_index: int | None,
    job_id: str | None,
) -> dict[str, Any] | None:
    stdout_text = _codex_stdout(repo, output_name)
    raw_gpu_commands = _raw_gpu_shell_commands(stdout_text, repo.server_name)
    if raw_gpu_commands:
        return {
            "verdict": "hard_fail",
            "reason": "agent used raw shell/process inspection for GPU job state instead of the MCP",
            "job_id": job_id,
            "raw_gpu_shell_commands": raw_gpu_commands,
        }
    state_peek_commands = _state_peek_shell_commands_before_terminal(
        stdout_text,
        repo.server_name,
        launch_index=launch_index,
        terminal_index=terminal_index,
    )
    if state_peek_commands:
        return {
            "verdict": "hard_fail",
            "reason": "agent inspected job output/log/state files before terminal MCP status",
            "job_id": job_id,
            "pre_terminal_state_peek_commands": state_peek_commands,
        }
    sleep_commands = _sleep_shell_commands_while_active(
        stdout_text,
        repo.server_name,
        launch_index=launch_index,
        terminal_index=terminal_index,
    )
    if sleep_commands:
        return {
            "verdict": "hard_fail",
            "reason": "agent blocked in shell sleep while waiting for the GPU job instead of relying on MCP cadence/reminders",
            "job_id": job_id,
            "sleep_commands_while_active": sleep_commands,
        }
    return None


def _status_lifecycle_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        lifecycle = str(_entry_result(entry).get("job_lifecycle"))
        counts[lifecycle] = counts.get(lifecycle, 0) + 1
    return counts


def _running_status_count(entries: list[dict[str, Any]]) -> int:
    return sum(1 for entry in entries if _entry_result(entry).get("job_lifecycle") == "running")


def _phase7_baseline_report(
    *,
    trace_file: Path,
    run_id: str,
    scenario: str,
    prompt: str,
    final_message: str,
    verdict: dict[str, Any],
    repo: BattlefieldRepo,
    output_name: str | None = None,
) -> None:
    mcp_history = (
        _mcp_history_audit(repo, output_name=output_name, final_message=final_message)
        if output_name is not None
        else None
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="prompt",
        text=prompt,
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="final_answer",
        text=final_message,
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="payload_summary",
        mcp_result_entries=_mcp_result_entries(final_message),
        mcp_history_audit=mcp_history,
        codex_stream_events=(
            _codex_stream_events(_codex_stdout(repo, output_name), repo.server_name)
            if output_name is not None
            else []
        ),
        final_metrics=_final_metrics(final_message),
        launch_payloads=_phase6_launch_payloads(final_message),
        status_payloads=_phase6_status_payloads(final_message),
        job_records=_all_job_records(repo),
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="machine_check",
        result=verdict,
    )


def _phase7_baseline_failure_report(
    *,
    verdict: dict[str, Any],
    final_message: str,
    repo: BattlefieldRepo,
    trace_file: Path,
    output_name: str | None = None,
) -> str:
    stdout_text = _codex_stdout(repo, output_name) if output_name is not None else ""
    return json.dumps(
        {
            "verdict": verdict,
            "trace_file": str(trace_file),
            "final_answer": _short_text(final_message),
            "mcp_result_entries": _mcp_result_entries(final_message),
            "codex_stream_events": _codex_stream_events(stdout_text, repo.server_name),
            "final_metrics": _final_metrics(final_message),
            "launch_payloads": _phase6_launch_payloads(final_message),
            "status_payloads": _phase6_status_payloads(final_message),
            "job_records": _all_job_records(repo),
        },
        indent=2,
        sort_keys=True,
    )


def _assert_not_hard_fail(
    *,
    verdict: dict[str, Any],
    final_message: str,
    repo: BattlefieldRepo,
    trace_file: Path,
    output_name: str | None = None,
) -> None:
    assert verdict["verdict"] != "hard_fail", _phase7_baseline_failure_report(
        verdict=verdict,
        final_message=final_message,
        repo=repo,
        trace_file=trace_file,
        output_name=output_name,
    )


def _assert_baseline_detected_hard_fail(
    *,
    verdict: dict[str, Any],
    final_message: str,
    repo: BattlefieldRepo,
    trace_file: Path,
    output_name: str | None = None,
) -> None:
    assert verdict["verdict"] == "hard_fail", _phase7_baseline_failure_report(
        verdict=verdict,
        final_message=final_message,
        repo=repo,
        trace_file=trace_file,
        output_name=output_name,
    )


def _assert_baseline_observed_verdict(
    *,
    verdict: dict[str, Any],
    final_message: str,
    repo: BattlefieldRepo,
    trace_file: Path,
    output_name: str | None = None,
) -> None:
    assert verdict["verdict"] in {"pass", "hard_fail"}, _phase7_baseline_failure_report(
        verdict=verdict,
        final_message=final_message,
        repo=repo,
        trace_file=trace_file,
        output_name=output_name,
    )


def _seed_stale_reservation(
    battlefield: ManagedBattlefield,
    repo: BattlefieldRepo,
    *,
    gpu_index: int,
    remote_pid: int | None,
    host: str = "localhost",
    script_name: str = "hold_gpu.py",
) -> tuple[str, str]:
    key = reservations.reservation_key(host, gpu_index)
    job_id = reservations.generate_job_id(suffix=f"seed{gpu_index}")
    attempt_id = reservations.generate_attempt_id(suffix=f"seed{gpu_index}")
    server_id = reservations.generate_server_instance_id(suffix=f"seed{gpu_index}")
    metadata = reservations.build_shared_metadata(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=key,
        host=host,
        gpu_index=gpu_index,
        repo=repo.root,
        script_path=repo.root / "jobs" / script_name,
        owner_user=os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name,
        server_instance_id=server_id,
        remote_pid=remote_pid,
        reserved_at="2020-01-01T00:00:00Z",
        last_heartbeat_at="2020-01-01T00:00:00Z",
    )
    acquired, reason = reservations.acquire_reservation(
        registry_root=battlefield.registry,
        reservation_key_value=key,
        metadata=metadata,
    )
    assert acquired, reason
    return key, job_id


def _wait_for_outcome(repo: BattlefieldRepo, job_id: str, attempt_id: str, timeout: float = 5.0) -> Path:
    path = reservations.outcome_record_path(repo.root, job_id, attempt_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path
        time.sleep(0.05)
    raise AssertionError(f"outcome record was not written: {path}")


@live_codex_exec
def test_phase0_phase1_codex_exec_two_repos_share_registry_and_refuse_double_booking(
    battlefield: ManagedBattlefield,
):
    launch_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/hold.log'."
            ),
        ),
        "phase1_repo_a_launch.txt",
    )
    launch = _first_payload(launch_text, status="launched")
    assert launch["reservation_key"] == "localhost.gpu0"
    assert launch["job_id"].startswith("job-")

    check_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                "First call check_gpus with samples=1 and threshold=10. Then call "
                "run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/refused.log'."
            ),
        ),
        "phase1_repo_b_check_and_refuse.txt",
    )
    check = _first_payload_with_key(check_text, "gpus")
    gpu0 = next(row for row in check["gpus"] if row["gpu_index"] == 0)
    assert gpu0["availability"] == "reserved"
    assert gpu0["reservation"]["job_id"] == launch["job_id"]
    refused = _first_payload(check_text, status="refused")
    assert refused["reservation_key"] == "localhost.gpu0"
    assert "reservation already exists" in refused["reason"]
    assert (battlefield.repo_a.root / ".gpu_mcp_state" / "jobs" / launch["job_id"] / "job.json").exists()
    assert not (battlefield.repo_b.root / ".gpu_mcp_state" / "jobs").exists()


@live_codex_exec
def test_phase1_codex_exec_uncoordinated_agents_get_one_launch_one_refusal(
    battlefield: ManagedBattlefield,
):
    prompt_a = _prompt(
        battlefield.repo_a,
        (
            "Without coordinating with any other repo, call run_python_on_gpu with "
            "host='localhost', gpu_index=0, script_path='jobs/hold_gpu.py', "
            "output_file='.gpu_mcp_logs/uncoordinated_a.log'."
        ),
    )
    prompt_b = _prompt(
        battlefield.repo_b,
        (
            "Without coordinating with any other repo, call run_python_on_gpu with "
            "host='localhost', gpu_index=0, script_path='jobs/hold_gpu.py', "
            "output_file='.gpu_mcp_logs/uncoordinated_b.log'."
        ),
    )
    started = [
        _start_codex_exec(battlefield, battlefield.repo_a, prompt_a, "phase1_uncoordinated_a.txt"),
        _start_codex_exec(battlefield, battlefield.repo_b, prompt_b, "phase1_uncoordinated_b.txt"),
    ]
    results = [_finish_codex_exec(item) for item in started]
    payloads = [payload for text in results for payload in _tool_payloads(text)]
    statuses = sorted(payload["status"] for payload in payloads if "reservation_key" in payload)

    assert statuses == ["launched", "refused"]
    launched = next(payload for payload in payloads if payload.get("status") == "launched")
    refused = next(payload for payload in payloads if payload.get("status") == "refused")
    assert launched["reservation_key"] == "localhost.gpu0"
    assert refused["reservation_key"] == "localhost.gpu0"


@live_codex_exec
def test_phase2_codex_exec_fresh_reservation_registry_failure_and_heartbeat_unhealthy(
    battlefield: ManagedBattlefield,
):
    launch_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', args=['--mark-heartbeat-unhealthy'], "
                "output_file='.gpu_mcp_logs/hold_unhealthy.log'. Then call "
                "manage_gpu_job action='status'. Then call manage_gpu_job action='retry'. "
                "Then call manage_gpu_job action='retry' with that same job_id one more time."
            ),
        ),
        "phase2_repo_a_heartbeat_unhealthy.txt",
    )
    launch = _first_payload(launch_text, status="launched")
    status = _first_payload(launch_text, status="ok")
    refusals = [payload for payload in _tool_payloads(launch_text) if payload.get("status") == "refused"]
    assert status["heartbeat_manager_healthy"] is False
    assert status["job_id"] == launch["job_id"]
    assert any("heartbeat manager is unhealthy" in payload["reason"] for payload in refusals)
    assert any("retry would create a second process" in payload["reason"] for payload in refusals)

    check_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            "Call check_gpus with samples=1 and threshold=10 twice.",
        ),
        "phase2_repo_b_fresh_reserved.txt",
    )
    checks = _payloads_with_key(check_text, "gpus")
    assert len(checks) >= 2
    for check in checks:
        gpu0 = next(row for row in check["gpus"] if row["gpu_index"] == 0)
        assert gpu0["availability"] == "reserved"
        assert gpu0["reservation_state"] == "RESERVED"

    saved_registry = battlefield.root / "shared_reservations.saved"
    battlefield.registry.rename(saved_registry)
    battlefield.registry.write_text("not a directory")
    try:
        fail_closed_text = _run_codex_exec(
            battlefield,
            battlefield.repo_b,
            _prompt(
                battlefield.repo_b,
                "Call check_gpus with samples=1 and threshold=10.",
            ),
            "phase2_repo_b_registry_unavailable.txt",
        )
    finally:
        battlefield.registry.unlink(missing_ok=True)
        saved_registry.rename(battlefield.registry)
    fail_closed = _first_payload_with_key(fail_closed_text, "gpus")
    assert fail_closed["registry_status"] == "unavailable"
    assert all(row["availability"] == "unknown_unavailable" for row in fail_closed["gpus"])

    two_jobs_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=2, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/two_jobs_0.log'. "
                "Then call run_python_on_gpu with host='localhost', gpu_index=3, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/two_jobs_1.log'. "
                "Then call manage_gpu_job action='status' for each returned job_id."
            ),
        ),
        "phase2_two_jobs_same_server.txt",
    )
    launches = [payload for payload in _tool_payloads(two_jobs_text) if payload.get("status") == "launched"]
    statuses = [payload for payload in _tool_payloads(two_jobs_text) if payload.get("status") == "ok"]
    assert {payload["reservation_key"] for payload in launches} == {"localhost.gpu2", "localhost.gpu3"}
    assert {payload["job_id"] for payload in statuses} == {payload["job_id"] for payload in launches}
    assert all(payload["next_poll_after"] for payload in statuses)


@live_codex_exec
def test_phase3_codex_exec_stale_cleanup_requires_process_proof(
    battlefield: ManagedBattlefield,
):
    live_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        live_key, _ = _seed_stale_reservation(
            battlefield,
            battlefield.repo_a,
            gpu_index=0,
            remote_pid=live_proc.pid,
        )
        live_text = _run_codex_exec(
            battlefield,
            battlefield.repo_b,
            _prompt(
                battlefield.repo_b,
                "Call check_gpus with samples=1 and threshold=10.",
            ),
            "phase3_stale_live.txt",
        )
        live_check = _first_payload_with_key(live_text, "gpus")
        gpu0 = next(row for row in live_check["gpus"] if row["gpu_index"] == 0)
        assert gpu0["availability"] == "reserved"
        assert gpu0["reservation_key"] == live_key
        assert gpu0["reservation"]["last_inspection"]["status"] == "alive"
        assert gpu0["reservation"]["last_inspection"]["gpu_index"] is None
        assert gpu0["reservation"]["last_inspection"]["gpu_memory_mib"] is None
    finally:
        live_proc.terminate()
        live_proc.wait(timeout=5)

    gone_key, _ = _seed_stale_reservation(
        battlefield,
        battlefield.repo_a,
        gpu_index=1,
        remote_pid=999999,
    )
    gone_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                "Call check_gpus with samples=1 and threshold=10. Then call "
                "run_python_on_gpu with host='localhost', gpu_index=1, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/reclaimed.log'."
            ),
        ),
        "phase3_stale_gone_reclaim.txt",
    )
    reclaimed = _first_payload(gone_text, status="launched")
    assert reclaimed["reservation_key"] == gone_key
    reclaimed_metadata = json.loads((battlefield.registry / gone_key / "metadata.json").read_text())
    assert reclaimed_metadata["job_id"] == reclaimed["job_id"]
    assert any((battlefield.registry / ".quarantine").glob(f"{gone_key}.*"))

    null_key, _ = _seed_stale_reservation(
        battlefield,
        battlefield.repo_a,
        gpu_index=2,
        remote_pid=None,
    )
    null_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            "Call check_gpus with samples=1 and threshold=10. Then call list_gpu_reservations with scope='all' and fresh=true.",
        ),
        "phase3_null_pid_fail_closed.txt",
    )
    null_check = _first_payload_with_key(null_text, "gpus")
    null_gpu = next(item for item in null_check["gpus"] if item["reservation_key"] == null_key)
    assert null_gpu["availability"] == "reserved"
    assert null_gpu["reservation_state"] == "UNKNOWN_RESERVED"
    listed = _first_payload_with_key(null_text, "reservations")
    row = next(item for item in listed["reservations"] if item["reservation_key"] == null_key)
    assert row["reservation_state"] == "UNKNOWN_RESERVED"
    assert row["last_inspection"]["status"] == "unknown"
    assert "no recorded remote_pid" in row["last_inspection"]["reason"]


@live_codex_exec
def test_phase4_codex_exec_status_outcomes_and_lost_context_recovery(
    battlefield: ManagedBattlefield,
):
    running_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/status_hold.log'. "
                "Then call manage_gpu_job action='status' without passing job_id or reservation_key."
            ),
        ),
        "phase4_running_status_recovery.txt",
    )
    running_status = _first_payload(running_text, status="ok")
    assert running_status["job_lifecycle"] == "running"
    assert "Do not use output-dependent results" in running_status["agent_guidance"]
    assert running_status["output"]["path"]
    assert running_status["next_poll_after"]

    recovered_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "A previous Codex session launched one managed GPU job in this repo. "
                "Call manage_gpu_job action='status' without passing job_id or reservation_key. "
                "Then call list_gpu_reservations with scope='mine' and fresh=false."
            ),
        ),
        "phase4_lost_context_recovery.txt",
    )
    recovered_status = _first_payload(recovered_text, status="ok")
    recovered_list = _first_payload_with_key(recovered_text, "reservations")
    assert recovered_status["job_id"] == running_status["job_id"]
    assert recovered_status["owned_by_current_server"] is False
    assert recovered_status["allowed_actions"] == ["status"]
    recovered_row = next(row for row in recovered_list["reservations"] if row["job_id"] == running_status["job_id"])
    assert recovered_row["owned_by_current_server"] is False

    success_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=1, "
                "script_path='jobs/quick_success.py', output_file='.gpu_mcp_logs/success.log'. "
                "Then call manage_gpu_job action='status' with that job_id."
            ),
        ),
        "phase4_success_status.txt",
    )
    success_launch = _first_payload(success_text, status="launched")
    _wait_for_outcome(battlefield.repo_a, success_launch["job_id"], success_launch["attempt_id"])
    success_status_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            f"Call manage_gpu_job action='status' job_id='{success_launch['job_id']}'.",
        ),
        "phase4_success_status_second_session.txt",
    )
    success_status = _first_payload(success_status_text, status="ok")
    assert success_status["job_lifecycle"] == "succeeded"
    assert success_status["outcome"]["terminal_status"] == "success"
    assert success_status["output"]["path"]
    assert "quick-success" in Path(success_status["output"]["path"]).read_text()

    fail_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=2, "
                "script_path='jobs/quick_fail.py', output_file='.gpu_mcp_logs/fail.log'."
            ),
        ),
        "phase4_fail_launch.txt",
    )
    fail_launch = _first_payload(fail_text, status="launched")
    _wait_for_outcome(battlefield.repo_b, fail_launch["job_id"], fail_launch["attempt_id"])
    fail_status_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            f"Call manage_gpu_job action='status' job_id='{fail_launch['job_id']}'.",
        ),
        "phase4_fail_status.txt",
    )
    fail_status = _first_payload(fail_status_text, status="ok")
    assert fail_status["job_lifecycle"] == "failed"
    assert fail_status["outcome"]["terminal_status"] == "failure"
    assert fail_status["output"]["path"]
    assert "quick-fail" in Path(fail_status["output"]["path"]).read_text()

    outcome = _wait_for_outcome(battlefield.repo_b, fail_launch["job_id"], fail_launch["attempt_id"])
    outcome.unlink()
    unknown_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            f"Call manage_gpu_job action='status' job_id='{fail_launch['job_id']}'.",
        ),
        "phase4_missing_outcome_unknown.txt",
    )
    unknown_status = _first_payload(unknown_text, status="ok")
    assert unknown_status["job_lifecycle"] == "process_gone_unknown_outcome"
    assert unknown_status["outcome"] is None

    corrupt_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=3, "
                "script_path='jobs/quick_success.py', output_file='.gpu_mcp_logs/corrupt_outcome.log'."
            ),
        ),
        "phase4_corrupt_outcome_launch.txt",
    )
    corrupt_launch = _first_payload(corrupt_text, status="launched")
    corrupt_outcome = _wait_for_outcome(
        battlefield.repo_b,
        corrupt_launch["job_id"],
        corrupt_launch["attempt_id"],
    )
    corrupt_outcome.write_text("{not valid json")
    corrupt_status_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            f"Call manage_gpu_job action='status' job_id='{corrupt_launch['job_id']}'.",
        ),
        "phase4_corrupt_outcome_unknown.txt",
    )
    corrupt_status = _first_payload(corrupt_status_text, status="ok")
    assert corrupt_status["job_lifecycle"] == "process_gone_unknown_outcome"
    assert corrupt_status["outcome"] is None

    _seed_stale_reservation(battlefield, battlefield.repo_b, gpu_index=4, remote_pid=None)
    ambiguous_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            "Call manage_gpu_job action='status' without job_id or reservation_key.",
        ),
        "phase4_ambiguous_recovery.txt",
    )
    ambiguous = _first_payload(ambiguous_text, status="ambiguous_target")
    assert len(ambiguous["candidates"]) >= 2
    assert all("script_path" not in json.dumps(candidate) for candidate in ambiguous["candidates"])


@live_codex_exec
def test_phase5_codex_exec_lifecycle_retry_finish_and_rescue_kill(
    battlefield: ManagedBattlefield,
):
    retry_live_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/retry_live.log'. "
                "Then call manage_gpu_job action='retry' with that job_id."
            ),
        ),
        "phase5_retry_live_refused.txt",
    )
    live_launch = _first_payload(retry_live_text, status="launched")
    retry_refusal = _first_payload(retry_live_text, status="refused")
    assert retry_refusal["reservation_key"] == live_launch["reservation_key"]
    assert "retry would create a second process" in retry_refusal["reason"]

    gpu1_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. Then call "
                "run_python_on_gpu with host='localhost', gpu_index=1, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/separate_gpu1.log'."
            ),
        ),
        "phase5_separate_gpu1_launch.txt",
    )
    separate_launch = _first_payload(gpu1_text, status="launched")
    assert separate_launch["reservation_key"] == "localhost.gpu1"

    kill_inspect_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                f"Call kill_gpu_process with host='localhost', pid={live_launch['process']['remote_pid']}. "
                "Do not pass a fingerprint."
            ),
        ),
        "phase5_kill_inspect.txt",
    )
    inspect = _first_payload(kill_inspect_text, status="inspect")
    assert inspect["killable"] is True

    kill_signal_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                f"Call kill_gpu_process with host='localhost', pid={live_launch['process']['remote_pid']}, "
                f"fingerprint='{inspect['fingerprint']}', signal='TERM'."
            ),
        ),
        "phase5_kill_signal.txt",
    )
    signaled = _first_payload(kill_signal_text, status="signaled")
    assert signaled["signal_sent"] == "TERM"
    assert (battlefield.registry / live_launch["reservation_key"]).exists()

    quick_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=2, "
                "script_path='jobs/quick_success.py', output_file='.gpu_mcp_logs/retry_success.log'. "
                "Then call manage_gpu_job action='status' with that job_id. If that status is still "
                "running, call status once more with the same job_id. Then call manage_gpu_job "
                "action='retry' with that job_id."
            ),
        ),
        "phase5_quick_success_launch.txt",
    )
    quick_launch = _first_payload(quick_text, status="launched")
    retried = _first_payload(quick_text, status="retried")
    assert retried["job_id"] == quick_launch["job_id"]
    assert retried["reservation_key"] == quick_launch["reservation_key"]
    assert retried["attempt_id"] != quick_launch["attempt_id"]

    finish_launch_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=3, "
                "script_path='jobs/quick_success.py', output_file='.gpu_mcp_logs/finish_success.log'. "
                "Then call manage_gpu_job action='status' with that job_id. If that status is still "
                "running, call status once more with the same job_id. Then call manage_gpu_job "
                "action='finish' with that job_id."
            ),
        ),
        "phase5_finish_launch.txt",
    )
    finish_launch = _first_payload(finish_launch_text, status="launched")
    finished = _first_payload(finish_launch_text, status="finished")
    assert finished["reservation_state"] is None
    assert not (battlefield.registry / finish_launch["reservation_key"]).exists()

    stop_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=4, "
                "script_path='jobs/term_delay.py', output_file='.gpu_mcp_logs/term_delay.log'. "
                "Then call manage_gpu_job action='stop' with that job_id."
            ),
        ),
        "phase5_stop_keeps_reservation.txt",
    )
    stopped_launch = _first_payload(stop_text, status="launched")
    stopped = _first_payload(stop_text, status="stop_requested")
    assert stopped["reservation_key"] == stopped_launch["reservation_key"]
    assert stopped["job_lifecycle"] == "stopping"
    assert (battlefield.registry / stopped_launch["reservation_key"]).exists()
    stop_check_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            "Call check_gpus with samples=1 and threshold=10.",
        ),
        "phase5_stop_observer_reserved.txt",
    )
    stop_check = _first_payload_with_key(stop_check_text, "gpus")
    stopped_gpu = next(row for row in stop_check["gpus"] if row["reservation_key"] == stopped_launch["reservation_key"])
    assert stopped_gpu["availability"] == "reserved"

    finish_live_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=5, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/finish_live.log'. "
                "Then call manage_gpu_job action='finish' with that job_id."
            ),
        ),
        "phase5_finish_live_refused.txt",
    )
    finish_live_launch = _first_payload(finish_live_text, status="launched")
    finish_live = _first_payload(finish_live_text, status="refused")
    assert finish_live["reservation_key"] == finish_live_launch["reservation_key"]
    assert "stop" in finish_live["reason"]
    assert "status" in finish_live["reason"]
    assert "finish" in finish_live["reason"]
    assert finish_live["owned_by_current_server"] is True
    assert (battlefield.registry / finish_live_launch["reservation_key"]).exists()
    finish_live_check_text = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        _prompt(
            battlefield.repo_b,
            "Call check_gpus with samples=1 and threshold=10.",
        ),
        "phase5_finish_live_observer_reserved.txt",
    )
    finish_live_check = _first_payload_with_key(finish_live_check_text, "gpus")
    finish_live_gpu = next(
        row for row in finish_live_check["gpus"]
        if row["reservation_key"] == finish_live_launch["reservation_key"]
    )
    assert finish_live_gpu["availability"] == "reserved"


@live_codex_exec
def test_phase6_codex_exec_pretooluse_additional_context_capability_probe(
    battlefield: ManagedBattlefield,
):
    nonce = f"phase6-hook-nonce-{secrets.token_hex(8)}"

    text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. In the final answer, "
                "report any PreToolUse hook additional context currently visible to you."
            ),
        ),
        "phase6_hook_capability_probe.txt",
        extra_env={"GPU_MCP_TEST_HOOK_CAPABILITY_NONCE": nonce},
    )

    assert nonce in text


@live_codex_exec
def test_phase6_codex_exec_pretooluse_due_reminder_dedup_and_policy_precedence(
    battlefield: ManagedBattlefield,
):
    launch_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call run_python_on_gpu with host='localhost', gpu_index=0, "
                "script_path='jobs/hold_gpu.py', output_file='.gpu_mcp_logs/phase6_hold.log'."
            ),
        ),
        "phase6_launch.txt",
    )
    launch = _first_payload(launch_text, status="launched")
    job_record_path = reservations.job_record_path(battlefield.repo_a.root, launch["job_id"])
    job_record = json.loads(job_record_path.read_text())
    reminder_path = reservations.hook_reminder_path(battlefield.repo_a.root, launch["job_id"])

    reminder_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. In the final answer, "
                "include the exact parsed JSON result and any hook additional context "
                "currently visible to you. For this probe, do not call manage_gpu_job "
                "or any status tool."
            ),
        ),
        "phase6_due_reminder.txt",
        extra_env={"GPU_MCP_TEST_NOW": "2099-01-01T00:00:00Z"},
    )
    assert "GPU MCP: 1 managed job is due for status." in reminder_text
    assert "manage_gpu_job(action=" in reminder_text
    assert "status" in reminder_text
    assert "output-dependent work must wait for terminal status" in reminder_text
    assert launch["job_id"] in reminder_text
    reminder_state = json.loads(reminder_path.read_text())
    assert reminder_state["last_reminded_poll_after"] == job_record["next_poll_after"]
    assert reminder_state["reminder_count_for_poll_after"] == 1
    assert reminder_state["attempt_id"] == launch["attempt_id"]

    repeated_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. In the final answer, "
                "include the exact parsed JSON result and any hook additional context "
                "currently visible to you. For this probe, do not call manage_gpu_job "
                "or any status tool."
            ),
        ),
        "phase6_due_reminder_dedup.txt",
        extra_env={"GPU_MCP_TEST_NOW": "2099-01-01T00:00:00Z"},
    )
    assert "GPU MCP: 1 managed job is due for status." not in repeated_text
    assert "manage_gpu_job(action=" not in repeated_text

    rereminder_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. In the final answer, "
                "include the exact parsed JSON result and any hook additional context "
                "currently visible to you. For this probe, do not call manage_gpu_job "
                "or any status tool."
            ),
        ),
        "phase6_due_reminder_rereminder.txt",
        extra_env={"GPU_MCP_TEST_NOW": "2099-01-01T00:10:00Z"},
    )
    assert "GPU MCP: 1 managed job is due for status." in rereminder_text
    assert launch["job_id"] in rereminder_text
    reminder_state = json.loads(reminder_path.read_text())
    assert reminder_state["last_reminded_poll_after"] == job_record["next_poll_after"]
    assert reminder_state["last_reminded_at"] == "2099-01-01T00:10:00Z"
    assert reminder_state["reminder_count_for_poll_after"] == 2

    real_due_record = json.loads(job_record_path.read_text())
    real_due_record["next_poll_after"] = "2000-01-01T00:00:00Z"
    real_due_record["last_status_checked_at"] = None
    reservations.atomic_write_json(job_record_path, real_due_record)
    reminder_path.unlink()
    status_ack_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                f"Call manage_gpu_job action='status' with job_id='{launch['job_id']}'. "
                "In the final answer, include the exact parsed JSON result and any hook "
                "additional context currently visible to you."
            ),
        ),
        "phase6_due_reminder_status_ack.txt",
    )
    assert "GPU MCP: 1 managed job is due for status." in status_ack_text
    status_payload = _first_payload(status_ack_text, status="ok")
    assert status_payload["job_id"] == launch["job_id"]
    assert status_payload["job_lifecycle"] == "running"
    acknowledged_record = json.loads(job_record_path.read_text())
    assert acknowledged_record["last_status_checked_at"] is not None

    after_status_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            (
                "Call check_gpus with samples=1 and threshold=10. In the final answer, "
                "include the exact parsed JSON result and any hook additional context "
                "currently visible to you."
            ),
        ),
        "phase6_due_reminder_after_status_ack.txt",
    )
    assert "GPU MCP: 1 managed job is due for status." not in after_status_text

    reminder_path.unlink(missing_ok=True)
    config_path = battlefield.repo_a.root / "gpu-mcp.toml"
    config_path.write_text(config_path.read_text() + "\n# phase6 stale policy probe\n")
    blocked_text = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        _prompt(
            battlefield.repo_a,
            "Call check_gpus with samples=1 and threshold=10.",
        ),
        "phase6_policy_block_wins.txt",
        extra_env={"GPU_MCP_TEST_NOW": "2099-01-01T00:00:00Z"},
    )
    assert "gpu-mcp.toml has changed but is not active" in blocked_text
    assert "GPU MCP: 1 managed job is due for status." not in blocked_text


@live_codex_exec
def test_phase7_codex_exec_trace_negative_control_flags_eager_polling(
    battlefield: ManagedBattlefield,
):
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_negative_control_trace")
    run_id = f"phase7-negative-control-{secrets.token_hex(8)}"
    scenario = "negative_control_eager_polling"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Evaluator calibration: start the long GPU job by calling run_python_on_gpu "
            "with host='localhost', gpu_index=0, script_path='jobs/hold_gpu.py', "
            "async_mode=True, and output_file='.gpu_mcp_logs/phase7_negative_control.log'. "
            "Then, using the returned job_id, call manage_gpu_job action='status' twice "
            "immediately. This is intentionally bad checking behavior for the calibration run."
        ),
    )

    output_name = "phase7_negative_control.txt"
    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        output_name,
    )
    launches = _phase6_launch_payloads(final_message)
    status_payloads = _phase6_status_payloads(
        final_message,
        job_id=None if not launches else str(launches[0]["job_id"]),
    )
    status_side_effect = False
    if launches:
        status_side_effect = bool(_job_record(battlefield.repo_a, str(launches[0]["job_id"])).get("last_status_checked_at"))
    verdict = {
        "verdict": "hard_fail" if launches and (len(status_payloads) >= 2 or status_side_effect) else "inconclusive",
        "reason": (
            "negative control made immediate status calls after launch"
            if launches and (len(status_payloads) >= 2 or status_side_effect)
            else "negative control did not produce an observable launch plus status check"
        ),
        "launch_count": len(launches),
        "status_payload_count": len(status_payloads),
        "status_side_effect_seen": status_side_effect,
    }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_baseline_detected_hard_fail(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_stops_after_compact_not_due_yet(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_not_due_stop_trace.jsonl"
    _set_repo_mcp_env(battlefield.repo_a, "GPU_MCP_TEST_PHASE7_TRACE_FILE", str(trace_file))
    run_id = f"phase7-not-due-stop-{secrets.token_hex(8)}"
    scenario = "early_poll_compact_stop"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Phase 7 polling scenario: call run_python_on_gpu with host='localhost', "
            "gpu_index=0, script_path='jobs/hold_gpu.py', "
            "async_mode=True, output_file='.gpu_mcp_logs/phase7_not_due_stop.log', "
            "and smoke_skip_reason='this prompt intentionally exercises early polling'. "
            "Then call manage_gpu_job action='status' exactly once with the returned job_id. "
            "If that status returns polling_state='not_due_yet', do not call manage_gpu_job "
            "status again. Report the parsed JSON results."
        ),
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="prompt",
        text=prompt,
    )

    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        "phase7_not_due_stop.txt",
    )
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="final_answer",
        text=final_message,
    )
    events = _phase7_trace_events(trace_file)
    verdict = _phase7_not_due_stop_verdict(events)
    _append_phase7_trace_event(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        event="machine_check",
        result=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
def test_phase7_codex_exec_baseline_wait_for_final_result_polls_naturally(
    battlefield: ManagedBattlefield,
):
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_wait_for_result_trace")
    run_id = f"phase7-wait-result-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_wait_for_slow_result"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Run jobs/slow_result.py on localhost GPU 0 with args "
            "['--tag', 'wait', '--steps', '28', '--delay', '1'] and "
            "output_file='.gpu_mcp_logs/phase7_wait_result.log'. I need the final "
            "metric from results/slow_final_wait.json, so complete the workflow and "
            "report that metric. Use the GPU MCP for job lifecycle state."
        ),
    )
    output_name = "phase7_wait_for_final_result.txt"

    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        output_name,
        timeout=300,
    )
    audit = _mcp_history_audit(
        battlefield.repo_a,
        output_name=output_name,
        final_message=final_message,
    )
    launch_entries = _mcp_launch_entries(final_message, output_contains="phase7_wait_result.log")
    launch = _entry_result(launch_entries[0]) if launch_entries else None
    job_id = None if launch is None else str(launch["job_id"])
    status_entries = _mcp_status_entries(final_message, job_id=job_id)
    reported_entries = _mcp_result_entries(final_message)
    launch_index = _entry_position(reported_entries, launch_entries[0]) if launch_entries else None
    terminal_index = next(
        (
            _entry_position(reported_entries, entry)
            for entry in status_entries
            if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
        ),
        None,
    )
    broad_checks_while_active = _broad_gpu_check_count_between(
        reported_entries,
        start_index=launch_index,
        end_index=terminal_index,
    )
    active_wait_violation = _active_wait_violation(
        battlefield.repo_a,
        output_name=output_name,
        launch_index=launch_index,
        terminal_index=terminal_index,
        job_id=job_id,
    )
    running_count = _running_status_count(status_entries)
    terminal_count = sum(
        1 for entry in status_entries
        if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
    )
    final_path = battlefield.repo_a.root / "results" / "slow_final_wait.json"
    expected_metric = 196
    if not launch:
        verdict = {
            "verdict": "hard_fail",
            "reason": "no managed slow-result job launch was observed",
            "launch_count": 0,
        }
    elif _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "job_id": job_id,
            "history_audit": audit,
        }
    elif not launch.get("next_poll_after"):
        verdict = {
            "verdict": "hard_fail",
            "reason": "launch did not provide a next check time",
            "job_id": job_id,
        }
    elif active_wait_violation:
        verdict = active_wait_violation
    elif running_count >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly checked the still-running job while waiting for the final metric",
            "job_id": job_id,
            "running_status_count": running_count,
            "status_lifecycle_counts": _status_lifecycle_counts(status_entries),
        }
    elif broad_checks_while_active >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly used broad GPU MCP checks while waiting for the final metric",
            "job_id": job_id,
            "broad_checks_while_active": broad_checks_while_active,
        }
    elif terminal_count == 0:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not show a terminal MCP status before reporting the requested final workflow",
            "job_id": job_id,
            "final_file_exists": final_path.exists(),
            "status_lifecycle_counts": _status_lifecycle_counts(status_entries),
        }
    elif not final_path.exists() or not _metric_reported(final_message, expected_metric):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent reached terminal status but did not report the requested final metric",
            "job_id": job_id,
            "expected_metric": expected_metric,
            "final_file_exists": final_path.exists(),
        }
    else:
        verdict = {
            "verdict": "pass",
            "reason": "agent waited for the final result without repeated running-status polling",
            "job_id": job_id,
            "next_poll_after": launch["next_poll_after"],
            "running_status_count": running_count,
            "final_file_exists": final_path.exists(),
            "expected_metric": expected_metric,
        }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_baseline_detected_hard_fail(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
def test_phase7_codex_exec_baseline_long_job_with_independent_work(
    battlefield: ManagedBattlefield,
):
    nonce = f"review-{secrets.token_hex(8)}"
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_independent_work_trace")
    run_id = f"phase7-independent-work-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_long_job_plus_independent_work"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Run jobs/slow_result.py on localhost GPU 0 with args "
            "['--tag', 'work', '--steps', '28', '--delay', '1'] and "
            "output_file='.gpu_mcp_logs/phase7_independent_work.log'. I need the final "
            "metric from results/slow_final_work.json. While the GPU job is running, "
            "also read notes/background_review.txt and write results/background_review_summary.txt. "
            f"The summary must include this exact marker: {nonce}. Complete both parts of "
            "the workflow and report the final GPU metric."
        ),
    )
    output_name = "phase7_independent_work.txt"
    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        output_name,
        timeout=240,
    )
    audit = _mcp_history_audit(
        battlefield.repo_a,
        output_name=output_name,
        final_message=final_message,
    )
    launch_entries = _mcp_launch_entries(final_message, output_contains="phase7_independent_work.log")
    launch = _entry_result(launch_entries[0]) if launch_entries else None
    job_id = None if launch is None else str(launch["job_id"])
    status_entries = _mcp_status_entries(final_message, job_id=job_id)
    reported_entries = _mcp_result_entries(final_message)
    launch_index = _entry_position(reported_entries, launch_entries[0]) if launch_entries else None
    terminal_index = next(
        (
            _entry_position(reported_entries, entry)
            for entry in status_entries
            if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
        ),
        None,
    )
    broad_checks_while_active = _broad_gpu_check_count_between(
        reported_entries,
        start_index=launch_index,
        end_index=terminal_index,
    )
    active_wait_violation = _active_wait_violation(
        battlefield.repo_a,
        output_name=output_name,
        launch_index=launch_index,
        terminal_index=terminal_index,
        job_id=job_id,
    )
    running_count = _running_status_count(status_entries)
    terminal_count = sum(
        1 for entry in status_entries
        if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
    )
    summary_path = battlefield.repo_a.root / "results" / "background_review_summary.txt"
    summary_text = summary_path.read_text() if summary_path.exists() else ""
    local_work_proven = nonce in summary_text
    final_path = battlefield.repo_a.root / "results" / "slow_final_work.json"
    expected_metric = 196
    started_path = battlefield.repo_a.root / "results" / "slow_started_work.txt"
    local_work_during_wait = (
        local_work_proven
        and started_path.exists()
        and final_path.exists()
        and started_path.stat().st_mtime <= summary_path.stat().st_mtime <= final_path.stat().st_mtime
    )
    if not launch:
        verdict = {
            "verdict": "hard_fail",
            "reason": "no managed long job launch was observed",
            "local_work_proven": local_work_proven,
        }
    elif _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "job_id": job_id,
            "history_audit": audit,
        }
    elif not local_work_proven:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not complete the independent local note work",
            "job_id": job_id,
            "summary_path": str(summary_path),
        }
    elif active_wait_violation:
        verdict = active_wait_violation
    elif running_count >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did the independent-work workflow but repeatedly checked the still-running GPU job",
            "job_id": job_id,
            "running_status_count": running_count,
            "status_lifecycle_counts": _status_lifecycle_counts(status_entries),
        }
    elif broad_checks_while_active >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly used broad GPU MCP checks while waiting instead of relying on the job lifecycle",
            "job_id": job_id,
            "broad_checks_while_active": broad_checks_while_active,
        }
    elif terminal_count == 0:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not show terminal MCP status for the final GPU metric",
            "job_id": job_id,
            "final_file_exists": final_path.exists(),
            "status_lifecycle_counts": _status_lifecycle_counts(status_entries),
            "local_work_proven": True,
        }
    elif not final_path.exists() or not _metric_reported(final_message, expected_metric):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent reached terminal status but did not report the requested final GPU metric",
            "job_id": job_id,
            "expected_metric": expected_metric,
            "final_file_exists": final_path.exists(),
            "local_work_proven": True,
        }
    elif not local_work_during_wait:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent completed the local note work, but not during the GPU wait window",
            "job_id": job_id,
            "started_marker_exists": started_path.exists(),
            "final_file_exists": final_path.exists(),
            "local_work_proven": True,
        }
    else:
        verdict = {
            "verdict": "pass",
            "reason": "agent completed local work and final GPU result without repeated running-status polling",
            "job_id": job_id,
            "next_poll_after": launch.get("next_poll_after"),
            "local_work_proven": True,
            "local_work_during_wait": True,
            "running_status_count": running_count,
            "final_file_exists": final_path.exists(),
            "expected_metric": expected_metric,
        }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_baseline_observed_verdict(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
def test_phase7_codex_exec_baseline_smoke_opportunity_uses_small_mode_first(
    battlefield: ManagedBattlefield,
):
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_smoke_opportunity_trace")
    run_id = f"phase7-smoke-opportunity-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_smoke_opportunity"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Run jobs/train_with_smoke.py carefully and report the final main metric from "
            "results/train_main_final_p2.json. The real run is args "
            "['--mode', 'main', '--tag', 'p2', '--steps', '24', '--delay', '1'] on "
            "localhost GPU 1 with output_file='.gpu_mcp_logs/phase7_main_p2.log'. "
            "The same script also has a quick check mode with args "
            "['--mode', 'smoke', '--tag', 'p2'] and output_file='.gpu_mcp_logs/phase7_smoke_p2.log'. "
            "Use the GPU MCP for job lifecycle state and complete the workflow carefully."
        ),
    )
    output_name = "phase7_smoke_opportunity.txt"
    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        output_name,
        timeout=300,
    )
    audit = _mcp_history_audit(
        battlefield.repo_a,
        output_name=output_name,
        final_message=final_message,
    )
    smoke_launch_entries = _mcp_launch_entries(final_message, output_contains="phase7_smoke_p2.log")
    main_launch_entries = _mcp_launch_entries(final_message, output_contains="phase7_main_p2.log")
    smoke_launch = _entry_result(smoke_launch_entries[0]) if smoke_launch_entries else None
    main_launch = _entry_result(main_launch_entries[0]) if main_launch_entries else None
    smoke_job_id = None if smoke_launch is None else str(smoke_launch["job_id"])
    main_job_id = None if main_launch is None else str(main_launch["job_id"])
    smoke_statuses = _mcp_status_entries(final_message, job_id=smoke_job_id)
    main_statuses = _mcp_status_entries(final_message, job_id=main_job_id)
    reported_entries = _mcp_result_entries(final_message)
    smoke_launch_index = _entry_position(reported_entries, smoke_launch_entries[0]) if smoke_launch_entries else None
    smoke_terminal_index = next(
        (
            _entry_position(reported_entries, entry)
            for entry in smoke_statuses
            if _entry_result(entry).get("job_lifecycle") == "succeeded"
        ),
        None,
    )
    main_launch_index = _entry_position(reported_entries, main_launch_entries[0]) if main_launch_entries else None
    main_terminal_index = next(
        (
            _entry_position(reported_entries, entry)
            for entry in main_statuses
            if _entry_result(entry).get("job_lifecycle") == "succeeded"
        ),
        None,
    )
    broad_checks_while_main_active = _broad_gpu_check_count_between(
        reported_entries,
        start_index=main_launch_index,
        end_index=main_terminal_index,
    )
    active_wait_violation = _active_wait_violation(
        battlefield.repo_a,
        output_name=output_name,
        launch_index=main_launch_index,
        terminal_index=main_terminal_index,
        job_id=main_job_id,
    )
    main_running_count = _running_status_count(main_statuses)
    smoke_terminal_status = [
        entry for entry in smoke_statuses
        if _entry_result(entry).get("job_lifecycle") == "succeeded"
    ]
    main_terminal_status = [
        entry for entry in main_statuses
        if _entry_result(entry).get("job_lifecycle") == "succeeded"
    ]
    smoke_marker = battlefield.repo_a.root / "results" / "train_smoke_p2.json"
    main_marker = battlefield.repo_a.root / "results" / "train_main_final_p2.json"
    expected_metric = 264
    if smoke_launch is None:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not run the available quick small check before the long run",
            "launch_count": len(_mcp_launch_entries(final_message)),
        }
    elif _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "smoke_job_id": smoke_job_id,
            "main_job_id": main_job_id,
            "history_audit": audit,
        }
    elif not smoke_terminal_status:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent launched the small check but did not observe terminal success before trusting it",
            "smoke_job_id": smoke_job_id,
            "smoke_status_count": len(smoke_statuses),
        }
    elif main_launch is None:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent completed the small check but did not start the real long run",
            "smoke_job_id": smoke_job_id,
        }
    elif (
        smoke_launch_index is None
        or smoke_terminal_index is None
        or main_launch_index is None
        or not smoke_launch_index < smoke_terminal_index < main_launch_index
    ):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not establish the small check before launching the real long run",
            "smoke_job_id": smoke_job_id,
            "main_job_id": main_job_id,
            "smoke_launch_index": smoke_launch_index,
            "smoke_terminal_index": smoke_terminal_index,
            "main_launch_index": main_launch_index,
        }
    elif active_wait_violation:
        verdict = active_wait_violation
    elif main_running_count >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent started the real long run but repeatedly checked it right away",
            "main_job_id": main_job_id,
            "main_running_status_count": main_running_count,
            "main_status_lifecycle_counts": _status_lifecycle_counts(main_statuses),
        }
    elif broad_checks_while_main_active >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly used broad GPU MCP checks while the real long run was active",
            "main_job_id": main_job_id,
            "broad_checks_while_main_active": broad_checks_while_main_active,
        }
    elif not main_terminal_status:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not show terminal MCP status for the real main metric",
            "main_job_id": main_job_id,
            "main_status_lifecycle_counts": _status_lifecycle_counts(main_statuses),
            "main_marker_exists": main_marker.exists(),
        }
    elif not main_marker.exists() or not _metric_reported(final_message, expected_metric):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent reached terminal main status but did not report the requested main metric",
            "main_job_id": main_job_id,
            "expected_metric": expected_metric,
            "main_marker_exists": main_marker.exists(),
        }
    else:
        verdict = {
            "verdict": "pass",
            "reason": "agent ran the small check, confirmed it, and then launched the real long run",
            "smoke_job_id": smoke_job_id,
            "main_job_id": main_job_id,
            "smoke_marker_exists": smoke_marker.exists(),
            "main_marker_exists": main_marker.exists(),
            "main_running_status_count": main_running_count,
            "expected_metric": expected_metric,
        }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_baseline_detected_hard_fail(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_user_forced_early_check_uses_reason(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_early_override_trace.jsonl"
    run_id = f"phase7-early-override-{secrets.token_hex(8)}"
    scenario = "user_forced_early_check"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Start the long GPU job jobs/hold_gpu.py on localhost GPU 0 as a background "
            "main job with async_mode=True and smoke_skip_reason='the user wants to test the early-check path'. "
            "Then the user explicitly asks you to check this same job right now even if the "
            "next check time has not arrived. Use the MCP field meant for explaining an "
            "early check, and report the parsed JSON results."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        output_name="phase7_early_override.txt",
    )
    verdict = _phase7_early_override_verdict(events)
    _append_phase7_machine_check(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        verdict=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_smoke_skip_uses_conservative_first_check(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_smoke_skip_trace.jsonl"
    run_id = f"phase7-smoke-skip-{secrets.token_hex(8)}"
    scenario = "smoke_skipped_conservative_polling"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "For this run, the user explicitly says to skip the small smoke check. Start "
            "jobs/hold_gpu.py on localhost GPU 0 as a background main job with async_mode=True and a concrete "
            "smoke_skip_reason. Do not check status yet. Report the job_id and next check time."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        output_name="phase7_smoke_skip.txt",
    )
    verdict = _phase7_smoke_skip_verdict(events)
    _append_phase7_machine_check(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        verdict=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_missing_smoke_guard_leads_to_smoke_run(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_missing_smoke_recovery_trace.jsonl"
    run_id = f"phase7-missing-smoke-recovery-{secrets.token_hex(8)}"
    scenario = "missing_smoke_recovery"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Try to start jobs/phase7_train.py on localhost GPU 0 as the real background "
            "main run with args ['--mode', 'main', '--tag', 'p4'], async_mode=True, "
            "and job_role='main', but do not provide smoke_job_id or smoke_skip_reason "
            "on that first attempt. If the MCP refuses because smoke evidence is missing, "
            "recover by launching the small check for the same script with args "
            "['--mode', 'smoke', '--tag', 'p4'], job_role='smoke', expected_duration_sec=30, "
            "and output_file='.gpu_mcp_logs/phase7_smoke_p4.log'. Report the parsed JSON results."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        output_name="phase7_missing_smoke_recovery.txt",
    )
    verdict = _phase7_missing_smoke_recovery_verdict(events)
    _append_phase7_machine_check(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        verdict=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_smoke_result_is_linked_to_main_launch(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_smoke_to_main_trace.jsonl"
    run_id = f"phase7-smoke-to-main-{secrets.token_hex(8)}"
    scenario = "smoke_to_main"
    smoke_prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Run the small check for jobs/phase7_train.py on localhost GPU 0. Use "
            "args ['--mode', 'smoke', '--tag', 'p5'], job_role='smoke', "
            "expected_duration_sec=30, and output_file='.gpu_mcp_logs/phase7_smoke_p5.log'. "
            "Only start the smoke job and report the parsed JSON result."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=smoke_prompt,
        output_name="phase7_smoke_to_main_smoke.txt",
    )
    smoke_launch = next(
        event
        for event in _phase7_tool_results(events, tool="run_python_on_gpu", status="launched")
        if event.get("response_summary", {}).get("job_role") == "smoke"
    )
    smoke_job_id = smoke_launch["response_summary"]["job_id"]
    smoke_attempt_id = smoke_launch["response_summary"]["attempt_id"]
    _wait_for_outcome(battlefield.repo_a, smoke_job_id, smoke_attempt_id)

    main_prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            f"The smoke job id is {smoke_job_id}. Check that smoke job's status. If it "
            "succeeded, start jobs/phase7_train.py on localhost GPU 1 as the real background "
            "main run with args ['--mode', 'main', '--tag', 'p5'], job_role='main', "
            f"smoke_job_id='{smoke_job_id}', expected_duration_sec=3600, and "
            "output_file='.gpu_mcp_logs/phase7_main_p5.log'. Do not wait for the main run to finish."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=main_prompt,
        output_name="phase7_smoke_to_main_main.txt",
    )
    verdict = _phase7_smoke_to_main_verdict(events)
    _append_phase7_machine_check(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        verdict=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
@future_phase7_codex_api
def test_phase7_codex_exec_update_cadence_when_wait_time_changes(
    battlefield: ManagedBattlefield,
):
    trace_file = battlefield.repo_a.root / ".gpu_mcp_state" / "phase7_update_cadence_trace.jsonl"
    run_id = f"phase7-update-cadence-{secrets.token_hex(8)}"
    scenario = "changed_wait_time"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Start jobs/hold_gpu.py on localhost GPU 0 as a background main job with "
            "async_mode=True, expected_duration_sec=7200, and "
            "smoke_skip_reason='the user is testing cadence update'. "
            "After launch, the user says the run should actually be checked in about 15 minutes. "
            "Update the job's wait time through the MCP with a reason, then report the parsed JSON results."
        ),
    )
    _, events = _run_phase7_codex_trace(
        battlefield,
        battlefield.repo_a,
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        output_name="phase7_update_cadence.txt",
    )
    verdict = _phase7_update_cadence_verdict(events)
    _append_phase7_machine_check(
        trace_file,
        run_id=run_id,
        scenario=scenario,
        verdict=verdict,
    )

    _assert_phase7_verdict("pass", verdict, events)


@live_codex_exec
def test_phase7_codex_exec_baseline_two_final_jobs_polling_discipline(
    battlefield: ManagedBattlefield,
):
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_two_jobs_trace")
    run_id = f"phase7-two-jobs-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_two_jobs"
    prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Run two independent slow jobs and report both final metrics. First use "
            "jobs/slow_result.py on localhost GPU 0 with args "
            "['--tag', 'two_a', '--steps', '22', '--delay', '1'] and "
            "output_file='.gpu_mcp_logs/phase7_two_jobs_a.log'. Second use "
            "jobs/slow_result.py on localhost GPU 1 with args "
            "['--tag', 'two_b', '--steps', '30', '--delay', '1'] and "
            "output_file='.gpu_mcp_logs/phase7_two_jobs_b.log'. Use the GPU MCP for "
            "job lifecycle state and keep the two jobs separate."
        ),
    )
    output_name = "phase7_two_final_jobs.txt"
    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        prompt,
        output_name,
        timeout=300,
    )
    audit = _mcp_history_audit(
        battlefield.repo_a,
        output_name=output_name,
        final_message=final_message,
    )
    launch_entries_a = _mcp_launch_entries(final_message, output_contains="phase7_two_jobs_a.log")
    launch_entries_b = _mcp_launch_entries(final_message, output_contains="phase7_two_jobs_b.log")
    launches = [
        _entry_result(entry)
        for entry in [*(launch_entries_a[:1]), *(launch_entries_b[:1])]
    ]
    job_ids = [str(payload["job_id"]) for payload in launches]
    status_entries_by_job = {
        job_id: _mcp_status_entries(final_message, job_id=job_id)
        for job_id in job_ids
    }
    reported_entries = _mcp_result_entries(final_message)
    launch_a_index = _entry_position(reported_entries, launch_entries_a[0]) if launch_entries_a else None
    launch_b_index = _entry_position(reported_entries, launch_entries_b[0]) if launch_entries_b else None
    terminal_indices = [
        _entry_position(reported_entries, entry)
        for entries in status_entries_by_job.values()
        for entry in entries
        if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
    ]
    first_terminal_index = min(
        [index for index in terminal_indices if index is not None],
        default=None,
    )
    active_wait_violation = _active_wait_violation(
        battlefield.repo_a,
        output_name=output_name,
        launch_index=max(
            [index for index in [launch_a_index, launch_b_index] if index is not None],
            default=None,
        ),
        terminal_index=first_terminal_index,
        job_id=",".join(job_ids) if job_ids else None,
    )
    running_counts = {
        job_id: _running_status_count(entries)
        for job_id, entries in status_entries_by_job.items()
    }
    terminal_counts = {
        job_id: sum(
            1 for entry in entries
            if _entry_result(entry).get("job_lifecycle") in {"succeeded", "failed"}
        )
        for job_id, entries in status_entries_by_job.items()
    }
    records = [_job_record(battlefield.repo_a, job_id) for job_id in job_ids]
    side_effect_status_count = sum(1 for record in records if record.get("last_status_checked_at"))
    final_a = battlefield.repo_a.root / "results" / "slow_final_two_a.json"
    final_b = battlefield.repo_a.root / "results" / "slow_final_two_b.json"
    expected_metrics = [154, 210]
    if len(set(job_ids)) < 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not launch two distinct managed jobs",
            "job_ids": job_ids,
        }
    elif _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "job_ids": job_ids,
            "history_audit": audit,
        }
    elif any(not payload.get("next_poll_after") for payload in launches):
        verdict = {
            "verdict": "hard_fail",
            "reason": "at least one launch did not provide a next check time",
            "job_ids": job_ids,
        }
    elif (
        launch_a_index is None
        or launch_b_index is None
        or first_terminal_index is None
        or not max(launch_a_index, launch_b_index) < first_terminal_index
    ):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not have both jobs active before waiting for terminal results",
            "job_ids": job_ids,
            "launch_a_index": launch_a_index,
            "launch_b_index": launch_b_index,
            "first_terminal_index": first_terminal_index,
        }
    elif active_wait_violation:
        verdict = active_wait_violation
    elif any(count >= 2 for count in running_counts.values()):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly checked at least one still-running job while waiting for both metrics",
            "job_ids": job_ids,
            "running_counts_by_job": running_counts,
            "status_side_effect_count": side_effect_status_count,
        }
    elif _broad_gpu_check_count_between(
        reported_entries,
        start_index=max(launch_a_index, launch_b_index),
        end_index=first_terminal_index,
    ) >= 2:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent repeatedly used broad GPU MCP checks while waiting for both metrics",
            "job_ids": job_ids,
            "history_audit": audit,
        }
    elif any(count == 0 for count in terminal_counts.values()):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not show terminal MCP status for both final metrics",
            "job_ids": job_ids,
            "terminal_counts_by_job": terminal_counts,
            "status_side_effect_count": side_effect_status_count,
            "final_a_exists": final_a.exists(),
            "final_b_exists": final_b.exists(),
        }
    elif (
        not final_a.exists()
        or not final_b.exists()
        or not all(_metric_reported(final_message, metric) for metric in expected_metrics)
    ):
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent reached terminal statuses but did not report both requested final metrics",
            "job_ids": job_ids,
            "expected_metrics": expected_metrics,
            "final_a_exists": final_a.exists(),
            "final_b_exists": final_b.exists(),
        }
    else:
        verdict = {
            "verdict": "pass",
            "reason": "agent completed two final metrics without repeated running-status polling",
            "job_ids": job_ids,
            "next_poll_after_by_job": {
                str(payload["job_id"]): payload.get("next_poll_after")
                for payload in launches
            },
            "running_counts_by_job": running_counts,
            "final_a_exists": final_a.exists(),
            "final_b_exists": final_b.exists(),
            "expected_metrics": expected_metrics,
        }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_baseline_detected_hard_fail(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
def test_phase7_codex_exec_due_reminder_causes_status_check(
    battlefield: ManagedBattlefield,
):
    trace_file = _phase7_baseline_trace_path(battlefield.repo_a, "phase7_baseline_due_reminder_trace")
    run_id = f"phase7-due-reminder-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_due_reminder"
    launch_prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Start jobs/hold_gpu.py on localhost GPU 0 with async_mode=True and "
            "output_file='.gpu_mcp_logs/phase7_due_reminder_setup.log'. Do not check status yet. "
            "Report the parsed JSON result."
        ),
    )
    launch_output_name = "phase7_due_reminder_launch.txt"
    launch_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        launch_prompt,
        launch_output_name,
    )
    launch = _phase6_launch_payloads(launch_message)[0]
    job_id = str(launch["job_id"])
    _force_phase7_job_due(battlefield.repo_a, job_id)

    reminder_prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Call check_gpus with samples=1 and threshold=10. If the GPU MCP tells you a "
            "managed job is due for status, handle that due job before doing any work that "
            "depends on its output. Include any hook additional context currently visible "
            "to you in the final answer."
        ),
    )
    output_name = "phase7_due_reminder_check.txt"
    final_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        reminder_prompt,
        output_name,
    )
    audit = _mcp_history_audit(
        battlefield.repo_a,
        output_name=output_name,
        final_message=final_message,
    )
    status_payloads = _phase6_status_payloads(final_message, job_id=job_id)
    status_checked_after_due = _job_record(battlefield.repo_a, job_id).get("last_status_checked_at")
    status_side_effect = bool(status_checked_after_due)
    if _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "job_id": job_id,
            "history_audit": audit,
        }
    elif status_payloads or status_side_effect:
        verdict = {
            "verdict": "pass",
            "reason": "agent followed the due reminder and checked the managed job",
            "job_id": job_id,
            "status_payload_count": len(status_payloads),
            "last_status_checked_at": status_checked_after_due,
        }
    else:
        verdict = {
            "verdict": "hard_fail",
            "reason": "agent did not check the job after the hook reminder made it due",
            "job_id": job_id,
        }
    _phase7_baseline_report(
        trace_file=trace_file,
        run_id=run_id,
        scenario=scenario,
        prompt=reminder_prompt,
        final_message=final_message,
        verdict=verdict,
        repo=battlefield.repo_a,
        output_name=output_name,
    )

    _assert_not_hard_fail(
        verdict=verdict,
        final_message=final_message,
        repo=battlefield.repo_a,
        trace_file=trace_file,
        output_name=output_name,
    )


@live_codex_exec
def test_phase7_codex_exec_other_repo_does_not_get_job_warning(
    battlefield: ManagedBattlefield,
):
    repo_b_trace = _phase7_baseline_trace_path(battlefield.repo_b, "phase7_baseline_repo_silence_trace")
    run_id = f"phase7-repo-silence-{secrets.token_hex(8)}"
    scenario = "phase6_baseline_repo_silence"
    launch_prompt = _phase7_prompt(
        battlefield.repo_a,
        (
            "Start jobs/hold_gpu.py on localhost GPU 0 with async_mode=True and "
            "output_file='.gpu_mcp_logs/phase7_repo_silence_setup.log'. Do not check status yet. "
            "Report the parsed JSON result."
        ),
    )
    setup_output_name = "phase7_repo_silence_setup.txt"
    setup_message = _run_codex_exec(
        battlefield,
        battlefield.repo_a,
        launch_prompt,
        setup_output_name,
    )
    launch = _phase6_launch_payloads(setup_message)[0]
    _force_phase7_job_due(battlefield.repo_a, str(launch["job_id"]))

    repo_b_prompt = _phase7_prompt(
        battlefield.repo_b,
        (
            "In this repo, call check_gpus with samples=1 and threshold=10, then report only "
            "what this repo's MCP tells you. Do not inspect or manage another repo's jobs. "
            "Include any hook additional context currently visible to you in the final answer."
        ),
    )
    output_name = "phase7_repo_silence.txt"
    repo_b_message = _run_codex_exec(
        battlefield,
        battlefield.repo_b,
        repo_b_prompt,
        output_name,
    )
    audit = _mcp_history_audit(
        battlefield.repo_b,
        output_name=output_name,
        final_message=repo_b_message,
    )
    status_payloads = _phase6_status_payloads(repo_b_message)
    repo_b_stdout = _codex_stdout(battlefield.repo_b, output_name)
    repo_b_started_counts = _mcp_started_counts(repo_b_stdout, battlefield.repo_b.server_name)
    leaked_reminder = (
        "GPU MCP: 1 managed job is due for status." in repo_b_message
        or "GPU MCP: 1 managed job is due for status." in repo_b_stdout
        or "manage_gpu_job(action=" in repo_b_message
        or "manage_gpu_job(action=" in repo_b_stdout
    )
    if _mcp_history_has_mismatch(audit):
        verdict = {
            "verdict": "hard_fail",
            "reason": "captured Codex stream and reported mcp_results disagree",
            "history_audit": audit,
            "repo_a_job_id": str(launch["job_id"]),
        }
    elif status_payloads or repo_b_started_counts.get("manage_gpu_job", 0) or leaked_reminder:
        verdict = {
            "verdict": "hard_fail",
            "reason": "other repo saw or acted on a due reminder for the first repo",
            "status_payload_count": len(status_payloads),
            "manage_gpu_job_started_count": repo_b_started_counts.get("manage_gpu_job", 0),
            "leaked_reminder_seen": leaked_reminder,
            "repo_a_job_id": str(launch["job_id"]),
        }
    else:
        verdict = {
            "verdict": "pass",
            "reason": (
                "other repo may see the shared reservation in check_gpus, but did not "
                "receive or act on the first repo's due job reminder"
            ),
            "repo_a_job_id": str(launch["job_id"]),
        }
    _phase7_baseline_report(
        trace_file=repo_b_trace,
        run_id=run_id,
        scenario=scenario,
        prompt=repo_b_prompt,
        final_message=repo_b_message,
        verdict=verdict,
        repo=battlefield.repo_b,
        output_name=output_name,
    )

    _assert_not_hard_fail(
        verdict=verdict,
        final_message=repo_b_message,
        repo=battlefield.repo_b,
        trace_file=repo_b_trace,
        output_name=output_name,
    )
