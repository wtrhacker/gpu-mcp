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
    (repo / "results").mkdir(parents=True, exist_ok=True)
    (repo / ".gpu_mcp_logs").mkdir(parents=True, exist_ok=True)
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


def _first_payload(final_message: str, **matches: Any) -> dict[str, Any]:
    for payload in _tool_payloads(final_message):
        if all(payload.get(key) == value for key, value in matches.items()):
            return payload
    raise AssertionError(f"no payload matching {matches!r} in:\n{final_message}")


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
    status_payload = _first_payload(status_ack_text, status="running")
    assert status_payload["job_id"] == launch["job_id"]
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
