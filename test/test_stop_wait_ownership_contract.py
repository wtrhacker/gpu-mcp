"""The old research-hook probe stays readable without polling repository jobs."""
import json

import gpu_mcp_policy_hook as hook


def test_stop_wait_probe_never_reads_jobs_or_codex_config(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "find_policy_file", lambda *a: (_ for _ in ()).throw(AssertionError("probe inspected repository")))
    assert hook.probe_stop_wait_ownership(tmp_path) == {
        "schema_version": 1, "owner": "gpu_mcp_wait", "owns_stop": False, "job_ids": [],
    }


def test_stop_wait_probe_cli_is_machine_readable(tmp_path, capsys):
    assert hook.main(["--probe-stop-wait-ownership", "--cwd", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1, "owner": "gpu_mcp_wait", "owns_stop": False, "job_ids": [],
    }
