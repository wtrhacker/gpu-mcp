from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import multiprocessing as mp

import gpu_mcp_reservations as reservations


FIXED_NOW = datetime(2026, 5, 30, 12, 34, 56, tzinfo=timezone.utc)


def _acquire_worker(root: str, key: str, suffix: str, queue) -> None:
    metadata = reservations.build_shared_metadata(
        job_id=reservations.generate_job_id(now=FIXED_NOW, suffix=f"job{suffix}"),
        attempt_id=reservations.generate_attempt_id(now=FIXED_NOW, suffix=f"attempt{suffix}"),
        reservation_key_value=key,
        host="gpu-a",
        gpu_index=0,
        repo=Path(root) / f"repo-{suffix}",
        script_path="jobs/train.py",
        owner_user="tingran",
        server_instance_id=reservations.generate_server_instance_id(
            hostname=f"login{suffix}",
            pid=1000 + int(suffix),
            suffix=f"srv{suffix}",
        ),
    )
    try:
        acquired, reason = reservations.acquire_reservation(
            registry_root=root,
            reservation_key_value=key,
            metadata=metadata,
        )
    except Exception as exc:  # pragma: no cover - surfaced through queue
        queue.put(("error", str(exc)))
        return
    queue.put(("acquired" if acquired else "refused", reason))


def test_phase0_constants_are_materialized():
    assert reservations.DEFAULT_HEARTBEAT_INTERVAL_SEC == 600
    assert reservations.MIN_HEARTBEAT_INTERVAL_SEC == 60
    assert reservations.MAX_HEARTBEAT_INTERVAL_SEC == 3600
    assert reservations.STALE_MULTIPLIER == 3
    assert reservations.DEFAULT_RESERVATION_REGISTRY_ROOT == (
        Path.home() / "gpu-mcp" / "state" / "reservations"
    )
    assert reservations.REPO_JOB_STATE_RELATIVE == Path(".gpu_mcp_state") / "jobs"


def test_id_generators_have_stable_public_format():
    job_id = reservations.generate_job_id(now=FIXED_NOW, suffix="abc123")
    attempt_id = reservations.generate_attempt_id(now=FIXED_NOW, suffix="def456")
    server_id = reservations.generate_server_instance_id(
        hostname="Login01.Example.EDU",
        pid=4242,
        suffix="ghi789",
    )

    assert job_id == "job-20260530T123456Z-abc123"
    assert attempt_id == "attempt-20260530T123456Z-def456"
    assert server_id == "server-login01.example.edu-4242-ghi789"
    assert reservations.is_job_id(job_id)
    assert reservations.is_attempt_id(attempt_id)
    assert reservations.is_server_instance_id(server_id)


def test_canonical_reservation_key_rejects_path_like_hosts():
    assert reservations.reservation_key("GPU-A", 0) == "gpu-a.gpu0"
    assert reservations.reservation_key("user@gpu-b.example.edu", 12) == "gpu-b.example.edu.gpu12"
    assert reservations.is_reservation_key("gpu-a.gpu0")
    assert not reservations.is_reservation_key("..")
    assert not reservations.is_reservation_key("GPU-A.gpu0")
    assert not reservations.is_reservation_key("gpu-a.gpu01")

    for bad_host in ["", "../gpu-a", "gpu/a", "."]:
        with pytest.raises(ValueError):
            reservations.reservation_key(bad_host, 0)
    with pytest.raises(ValueError):
        reservations.reservation_key("gpu-a", True)


def test_test_registry_root_can_be_injected(tmp_path):
    injected = tmp_path / "reservations"
    assert reservations.reservation_registry_root(
        env={
            reservations.TEST_RESERVATION_ROOT_ENV: str(injected),
            "PYTEST_CURRENT_TEST": "test",
        }
    ) == injected.resolve()
    assert reservations.reservation_registry_root(
        env={reservations.TEST_RESERVATION_ROOT_ENV: str(injected)}
    ) == reservations.DEFAULT_RESERVATION_REGISTRY_ROOT


def test_shared_metadata_is_sanitized(tmp_path):
    repo = tmp_path / "repo"
    script = repo / "jobs" / "train_secret_dataset.py"
    repo.mkdir()
    metadata = reservations.build_shared_metadata(
        job_id="job-20260530T123456Z-abc123",
        attempt_id="attempt-20260530T123456Z-def456",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        repo=repo,
        script_path=script,
        owner_user="tingran",
        server_instance_id="server-login-1234-srv",
        remote_pid=12345,
        process_fingerprint="gpu-mcp-process:fingerprint",
        heartbeat_interval_sec=600,
    )

    assert metadata["repo"] == str(repo.resolve())
    assert metadata["script_name"] == "train_secret_dataset.py"
    assert metadata["remote_pid"] == 12345
    assert metadata["process_fingerprint"] == "gpu-mcp-process:fingerprint"
    assert "script_path" not in metadata
    assert "args" not in metadata
    assert "output_file" not in metadata
    assert str(script.resolve()) not in str(metadata)


def test_repo_local_job_and_outcome_paths(tmp_path):
    repo = tmp_path / "repo"
    job_id = "job-20260530T123456Z-abc123"
    attempt_id = "attempt-20260530T123456Z-def456"

    assert reservations.job_record_path(repo, job_id) == (
        repo.resolve() / ".gpu_mcp_state" / "jobs" / job_id / "job.json"
    )
    assert reservations.outcome_record_path(repo, job_id, attempt_id) == (
        repo.resolve()
        / ".gpu_mcp_state"
        / "jobs"
        / job_id
        / "attempts"
        / attempt_id
        / "outcome.json"
    )


def test_job_record_keeps_sensitive_data_repo_local(tmp_path):
    repo = tmp_path / "repo"
    script = repo / "jobs" / "train.py"
    output = repo / ".gpu_mcp_logs" / "train.log"
    record = reservations.build_job_record(
        job_id="job-20260530T123456Z-abc123",
        attempt_id="attempt-20260530T123456Z-def456",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        script_path=script,
        args=["--token", "secret"],
        output_file=output,
        server_instance_id="server-login-1234-srv",
        next_poll_after="2026-05-30T12:44:56Z",
    )

    assert record["script_path"] == str(script.resolve())
    assert record["args"] == ["--token", "secret"]
    assert record["output_file"] == str(output.resolve())
    assert record["active_attempt_id"] == "attempt-20260530T123456Z-def456"
    assert record["next_poll_after"] == "2026-05-30T12:44:56Z"


def test_outcome_record_contract():
    outcome = reservations.build_outcome_record(
        job_id="job-20260530T123456Z-abc123",
        attempt_id="attempt-20260530T123456Z-def456",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        remote_pid=12345,
        terminal_status="success",
        exit_code=0,
        signal=None,
        started_at="2026-05-30T12:34:56Z",
        ended_at="2026-05-30T12:35:01Z",
    )

    assert outcome == {
        "schema_version": 1,
        "job_id": "job-20260530T123456Z-abc123",
        "attempt_id": "attempt-20260530T123456Z-def456",
        "reservation_key": "gpu-a.gpu0",
        "host": "gpu-a",
        "gpu_index": 0,
        "remote_pid": 12345,
        "terminal_status": "success",
        "exit_code": 0,
        "signal": None,
        "started_at": "2026-05-30T12:34:56Z",
        "ended_at": "2026-05-30T12:35:01Z",
        "error_summary": None,
    }

    with pytest.raises(ValueError):
        reservations.build_outcome_record(
            job_id="job-20260530T123456Z-abc123",
            attempt_id="attempt-20260530T123456Z-def456",
            reservation_key_value="gpu-a.gpu0",
            host="gpu-a",
            gpu_index=0,
            terminal_status="guessed_success",
            ended_at="2026-05-30T12:35:01Z",
        )
    with pytest.raises(ValueError):
        reservations.build_outcome_record(
            job_id="bad",
            attempt_id="attempt-20260530T123456Z-def456",
            reservation_key_value="gpu-a.gpu0",
            host="gpu-a",
            gpu_index=0,
            terminal_status="success",
            ended_at="2026-05-30T12:35:01Z",
        )


def test_atomic_acquire_allows_only_one_process(tmp_path):
    key = "gpu-a.gpu0"
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_acquire_worker, args=(str(tmp_path), key, str(index), queue))
        for index in range(4)
    ]

    for process in processes:
        process.start()
    results = [queue.get(timeout=5) for _ in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert [status for status, _ in results].count("acquired") == 1
    assert [status for status, _ in results].count("refused") == 3
    assert (tmp_path / key / "metadata.json").exists()


def test_acquire_rejects_symlinked_registry_root(tmp_path):
    target = tmp_path / "real_registry"
    target.mkdir()
    link = tmp_path / "registry_link"
    link.symlink_to(target)
    metadata = reservations.build_shared_metadata(
        job_id="job-20260530T123456Z-abc123",
        attempt_id="attempt-20260530T123456Z-def456",
        reservation_key_value="gpu-a.gpu0",
        host="gpu-a",
        gpu_index=0,
        repo=tmp_path / "repo",
        script_path="jobs/train.py",
        owner_user="tingran",
        server_instance_id="server-login-1234-srv",
    )

    with pytest.raises(OSError, match="must not be a symlink"):
        reservations.acquire_reservation(
            registry_root=link,
            reservation_key_value="gpu-a.gpu0",
            metadata=metadata,
        )
