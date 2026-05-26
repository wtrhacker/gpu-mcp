from __future__ import annotations

"""Contracts for human-approved GPU MCP policy edit/reload lifecycle."""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER = REPO_ROOT / "gpu_mcp_server.py"


@pytest.fixture()
def repo_fixture(tmp_path):
    repo = tmp_path / "repo"
    (repo / "jobs").mkdir(parents=True)
    (repo / "results").mkdir()
    (repo / ".gpu_mcp_logs").mkdir()
    return repo


def _write_config(repo: Path, *, nodes: list[str] | None = None, write_roots: list[str] | None = None) -> Path:
    config = repo / "gpu-mcp.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                f"nodes = {nodes or ['gpu-a']!r}",
                "script_roots = ['jobs']",
                f"write_roots = {write_roots or ['results']!r}",
                "output_roots = ['.gpu_mcp_logs']",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                "sync_timeout_sec = 5",
                "",
            ]
        )
    )
    return config


def _import_server(monkeypatch, config: Path, store: Path):
    monkeypatch.delenv("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", raising=False)
    approval = importlib.import_module("gpu_mcp_policy_approval")
    monkeypatch.setattr(approval, "default_store_path", lambda: store)
    sys.modules.pop("gpu_mcp_server", None)
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--config", str(config)])
    return importlib.import_module("gpu_mcp_server")


@pytest.fixture()
def approval_module():
    return importlib.import_module("gpu_mcp_policy_approval")


def test_approved_policy_record_stores_hash_summary_and_history(
    approval_module, repo_fixture, tmp_path
):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)

    record = approval_module.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    data = json.loads(store.read_text())
    entry = data[str(config.resolve())]

    assert record["current_hash"] == approval_module.policy_file_hash(config)
    assert entry["current_hash"] == record["current_hash"]
    assert entry["history"][-1]["hash"] == record["current_hash"]
    assert entry["history"][-1]["approved_by"] == "human"
    assert entry["history"][-1]["summary"]["nodes"] == ["gpu-a"]
    assert entry["history"][-1]["diff_summary"] == ["initial approval"]


def test_default_approved_policy_store_lives_under_gpu_mcp_state(approval_module):
    assert approval_module.default_store_path() == Path.home() / "gpu-mcp" / "state" / "approved-policies.json"


def test_approved_policy_check_rejects_changed_policy(
    approval_module, repo_fixture, tmp_path
):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval_module.approve_policy(policy, store_path=store, diff_summary=["initial approval"])

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])

    with pytest.raises(approval_module.PolicyApprovalError, match="not approved|changed"):
        approval_module.verify_policy_approved(config, store_path=store)


def test_server_startup_requires_approved_policy(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    monkeypatch.setattr(approval, "default_store_path", lambda: store)
    monkeypatch.delenv("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", raising=False)
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--config", str(config)])
    sys.modules.pop("gpu_mcp_server", None)

    with pytest.raises(SystemExit) as excinfo:
        importlib.import_module("gpu_mcp_server")

    assert excinfo.value.code == 2


def test_policy_approval_bypass_is_pytest_only(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    monkeypatch.setattr(approval, "default_store_path", lambda: store)
    monkeypatch.setenv("GPU_MCP_TEST_DISABLE_POLICY_APPROVAL", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--config", str(config)])
    sys.modules.pop("gpu_mcp_server", None)

    with pytest.raises(SystemExit) as excinfo:
        importlib.import_module("gpu_mcp_server")

    assert excinfo.value.code == 2


def test_preview_policy_reload_reports_diff_and_token(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    result = json.loads(server.preview_policy_reload())

    assert result["status"] == "preview"
    assert result["validation"] == "pass"
    assert result["active_hash"] != result["candidate_hash"]
    assert "gpu-b" in " ".join(result["diff_summary"])
    assert result["reload_token"].startswith("gpu-mcp-reload-v1:")
    instructions = "\n".join(result["agent_instructions"])
    assert "Show this raw preview output" in instructions
    assert "diff_summary and hashes" in instructions
    assert "Do not summarize it as the only evidence" in instructions
    assert "explicit human approval" in instructions
    assert "If you edited gpu-mcp.toml yourself" in instructions


def test_preview_policy_reload_reports_invalid_policy_without_token(
    monkeypatch, repo_fixture, tmp_path
):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    config.write_text("this is not = valid = toml\n")
    result = json.loads(server.preview_policy_reload())

    assert result["status"] == "error"
    assert result["validation"] == "fail"
    assert "reload_token" not in result


def test_reload_policy_requires_token_and_updates_active_policy(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    preview = json.loads(server.preview_policy_reload())
    refused = json.loads(server.reload_policy(token="wrong-token"))
    reloaded = json.loads(server.reload_policy(token=preview["reload_token"]))

    assert refused["status"] == "refused"
    assert reloaded["status"] == "reloaded"
    assert "gpu-b" in server.NODES
    assert approval.verify_policy_approved(config, store_path=store)["current_hash"] == preview["candidate_hash"]
    assert json.loads(server.reload_policy(token=preview["reload_token"]))["status"] == "refused"


def test_reject_policy_reload_invalidates_preview_token_without_reloading(
    monkeypatch, repo_fixture, tmp_path
):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    preview = json.loads(server.preview_policy_reload())
    rejected = json.loads(server.reject_policy_reload(token=preview["reload_token"]))
    reload_result = json.loads(server.reload_policy(token=preview["reload_token"]))

    assert rejected["status"] == "rejected"
    assert rejected["active_hash"] == preview["active_hash"]
    assert "gpu-b" not in server.NODES
    assert reload_result["status"] == "refused"
    assert "invalid or already used" in reload_result["reason"]


def test_reload_policy_refuses_if_policy_changes_after_preview(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    preview = json.loads(server.preview_policy_reload())
    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"], write_roots=["results", "/tmp/gpu-mcp-test"])

    result = json.loads(server.reload_policy(token=preview["reload_token"]))

    assert result["status"] == "refused"
    assert "changed after preview" in result["reason"]


def test_reload_policy_refuses_expired_preview_token(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    preview = json.loads(server.preview_policy_reload())
    server.PENDING_POLICY_RELOADS[preview["reload_token"]]["created_at"] = (
        time.monotonic() - server.POLICY_RELOAD_TOKEN_TTL_SEC - 1
    )

    result = json.loads(server.reload_policy(token=preview["reload_token"]))

    assert result["status"] == "refused"
    assert "expired" in result["reason"]
    assert "gpu-b" not in server.NODES


def test_preview_policy_reload_caps_pending_tokens(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)
    server.POLICY_RELOAD_MAX_PENDING = 3

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    tokens = [json.loads(server.preview_policy_reload())["reload_token"] for _ in range(5)]

    assert len(server.PENDING_POLICY_RELOADS) == 3
    assert tokens[0] not in server.PENDING_POLICY_RELOADS
    assert tokens[1] not in server.PENDING_POLICY_RELOADS
    assert tokens[-1] in server.PENDING_POLICY_RELOADS


def test_normal_tools_refuse_when_policy_file_is_stale(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])

    results = [
        server.check_gpus(),
        server.cluster_info(),
        server.check_gpu_processes(),
        server.kill_gpu_process(host="gpu-a", pid=123),
        server.run_python_on_gpu(host="gpu-a", gpu_index=0, script_path="jobs/ok.py"),
    ]

    for result in results:
        text = result if isinstance(result, str) else json.dumps(result)
        assert "gpu-mcp.toml has changed but has not been reloaded" in text
        assert "Do not revert the file" in text
        assert "Stop immediately and explain" in text
        assert "preview_policy_reload" in text


def test_reload_tools_are_allowed_when_policy_file_is_stale(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)

    _write_config(repo_fixture, nodes=["gpu-a", "gpu-b"])
    preview = json.loads(server.preview_policy_reload())
    rejected = json.loads(server.reject_policy_reload(token=preview["reload_token"]))

    assert preview["status"] == "preview"
    assert preview["candidate_hash"] != preview["active_hash"]
    assert rejected["status"] == "rejected"
    assert "gpu-b" not in server.NODES


def test_stale_policy_check_memoizes_unchanged_file_stat(monkeypatch, repo_fixture, tmp_path):
    config = _write_config(repo_fixture)
    store = tmp_path / "approved-policies.json"
    approval = importlib.import_module("gpu_mcp_policy_approval")
    policy = importlib.import_module("gpu_mcp_config").load_policy(config)
    approval.approve_policy(policy, store_path=store, diff_summary=["initial approval"])
    server = _import_server(monkeypatch, config, store)
    calls = {"count": 0}
    real_hash = server.policy_file_hash

    def counting_hash(path):
        calls["count"] += 1
        return real_hash(path)

    monkeypatch.setattr(server, "policy_file_hash", counting_hash)
    server._STALE_POLICY_HASH_CACHE = {"signature": None, "hash": ""}

    assert server._stale_policy_refusal() is None
    assert server._stale_policy_refusal() is None

    assert calls["count"] == 1
