import json

import gpu_mcp_policy_hook as hook
import gpu_mcp_reservations as reservations


def test_stop_wait_probe_reports_managed_owner_without_waiting(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = repo / "gpu-mcp.toml"
    policy.write_text("repo_root = 'unused'\n", encoding="utf-8")
    monkeypatch.setattr(hook, "find_policy_file", lambda _cwd: policy)
    monkeypatch.setattr(
        hook,
        "_stop_job_candidates",
        lambda _repo: [
            {"job_id": "job-20260816T120000Z-b"},
            {"job_id": "job-20260816T120000Z-a"},
        ],
    )

    result = hook.probe_stop_wait_ownership(repo)

    assert result == {
        "schema_version": 1,
        "owner": "gpu_mcp_wait",
        "owns_stop": True,
        "job_ids": [
            "job-20260816T120000Z-a",
            "job-20260816T120000Z-b",
        ],
    }


def test_stop_wait_probe_derives_ownership_from_durable_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    jobs = repo / "jobs"
    jobs.mkdir(parents=True)
    (repo / "gpu-mcp.toml").write_text("schema_version = 1\n", encoding="utf-8")
    script = jobs / "simulation.py"
    script.write_text("print('running')\n", encoding="utf-8")
    registry = tmp_path / "reservations"
    monkeypatch.setenv("GPU_MCP_TEST_RESERVATION_ROOT", str(registry))

    job_id = "job-20260816T120000Z-durable"
    attempt_id = "attempt-20260816T120000Z-durable"
    reservation_key = reservations.reservation_key("gpu-a", 0)
    server_id = "server-gpu-a-1234-durable"
    record = reservations.build_job_record(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key,
        host="gpu-a",
        gpu_index=0,
        script_path=script,
        args=[],
        output_file=repo / ".gpu_mcp_logs" / "simulation.log",
        server_instance_id=server_id,
        next_poll_after="2099-01-01T00:00:00Z",
    )
    metadata = reservations.build_shared_metadata(
        job_id=job_id,
        attempt_id=attempt_id,
        reservation_key_value=reservation_key,
        host="gpu-a",
        gpu_index=0,
        repo=repo,
        script_path=script,
        owner_user="tester",
        server_instance_id=server_id,
        remote_pid=12345,
        process_fingerprint="gpu-mcp-process:durable",
    )
    reservations.atomic_write_json(reservations.job_record_path(repo, job_id), record)
    acquired, _reason = reservations.acquire_reservation(
        registry_root=registry,
        reservation_key_value=reservation_key,
        metadata=metadata,
    )
    assert acquired is True

    assert hook.probe_stop_wait_ownership(repo) == {
        "schema_version": 1,
        "owner": "gpu_mcp_wait",
        "owns_stop": True,
        "job_ids": [job_id],
    }

    record["active_reservation"] = False
    reservations.atomic_write_json(reservations.job_record_path(repo, job_id), record)
    assert hook.probe_stop_wait_ownership(repo)["owns_stop"] is False


def test_stop_wait_probe_cli_is_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        hook,
        "probe_stop_wait_ownership",
        lambda _cwd: {
            "schema_version": 1,
            "owner": "gpu_mcp_wait",
            "owns_stop": False,
            "job_ids": [],
        },
    )

    assert hook.main(["--probe-stop-wait-ownership", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "owner": "gpu_mcp_wait",
        "owns_stop": False,
        "job_ids": [],
    }


def test_gpu_wakeup_returns_scientific_control_after_required_status():
    result = hook._stop_continuation(
        [
            {
                "job_id": "job-20260816T120000Z-example",
                "kind": "poll_due",
            }
        ]
    )

    reason = result["reason"]
    assert 'manage_gpu_job(action="status"' in reason
    assert "Status reconciles managed state" in reason
    assert "Wait again only" not in reason
