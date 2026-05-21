from __future__ import annotations

"""Executable contract for human-first SSH bootstrap.

These tests target the future bootstrap API, not the current site-specific
script behavior. Bootstrap is a one-time human setup step: it records which
hosts a dedicated SSH route can reach, but it never grants repo permission.

Expected future API:

- `gpu_mcp_bootstrap.BootstrapError`
- `gpu_mcp_bootstrap.bootstrap_hosts(hosts=None, hosts_file=None, inventory_path=...)`
- `gpu_mcp_bootstrap.load_inventory(path) -> dict`
- `gpu_mcp_config.load_policy(config_path) -> policy`
- `gpu_mcp_policy.validate_host(policy, host)`
"""

import importlib
import json
import uuid
from pathlib import Path

import jsonschema
import pytest


pytestmark = [
    pytest.mark.contract,
    pytest.mark.xfail(
        reason="human-first bootstrap contract is not implemented yet",
        strict=True,
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "test_mcp_repos" / "bootstrap_contract"
BOOTSTRAP_INVENTORY_SCHEMA = json.loads(
    (REPO_ROOT / "contracts" / "schemas" / "bootstrap-inventory.schema.json").read_text()
)


@pytest.fixture()
def modules():
    return (
        importlib.import_module("gpu_mcp_bootstrap"),
        importlib.import_module("gpu_mcp_config"),
        importlib.import_module("gpu_mcp_policy"),
    )


@pytest.fixture()
def fixture_root(request):
    root = FIXTURE_ROOT / f"{request.node.name}-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_repo_policy(repo: Path, nodes: list[str]) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "jobs").mkdir(exist_ok=True)
    (repo / "results").mkdir(exist_ok=True)
    config = repo / "gpu-mcp.toml"
    config.write_text(
        "\n".join(
            [
                "schema_version = 1",
                f"repo_root = {str(repo)!r}",
                f"nodes = {nodes!r}",
                "script_roots = ['jobs']",
                "write_roots = ['results']",
                "output_roots = ['.gpu_mcp_logs']",
                "allowed_gpu_names = []",
                "min_free_memory_mib = 0",
                "sync_timeout_sec = 5",
                "",
            ]
        )
    )
    return config


def _assert_bootstrap_inventory(inventory: dict) -> None:
    jsonschema.validate(instance=inventory, schema=BOOTSTRAP_INVENTORY_SCHEMA)
    assert inventory["schema_version"] == 1
    assert isinstance(inventory["hosts"], list)


def test_bootstrap_requires_explicit_hosts_or_hosts_file(modules, fixture_root):
    gpu_mcp_bootstrap, _, _ = modules

    with pytest.raises(gpu_mcp_bootstrap.BootstrapError, match="hosts"):
        gpu_mcp_bootstrap.bootstrap_hosts(
            inventory_path=fixture_root / "bootstrap_hosts.json"
        )


def test_bootstrap_rejects_hosts_and_hosts_file_together(modules, fixture_root):
    gpu_mcp_bootstrap, _, _ = modules
    hosts_file = fixture_root / "hosts.txt"
    hosts_file.write_text("gpu-a\n")

    with pytest.raises(gpu_mcp_bootstrap.BootstrapError, match="choose"):
        gpu_mcp_bootstrap.bootstrap_hosts(
            hosts=["gpu-a"],
            hosts_file=hosts_file,
            inventory_path=fixture_root / "bootstrap_hosts.json",
        )


def test_bootstrap_writes_inventory_for_verified_and_failed_hosts(modules, fixture_root):
    gpu_mcp_bootstrap, _, _ = modules
    inventory_path = fixture_root / "bootstrap_hosts.json"

    result = gpu_mcp_bootstrap.bootstrap_hosts(
        hosts=["gpu-a", "gpu-b"],
        inventory_path=inventory_path,
        ssh_probe={
            "gpu-a": {"status": "verified", "remote_hostname": "gpu-a"},
            "gpu-b": {"status": "failed", "error": "connection timed out"},
        },
    )

    inventory = json.loads(inventory_path.read_text())
    assert result == inventory
    _assert_bootstrap_inventory(inventory)
    assert inventory["hosts"][0]["host"] == "gpu-a"
    assert inventory["hosts"][0]["status"] == "verified"
    assert inventory["hosts"][1]["host"] == "gpu-b"
    assert inventory["hosts"][1]["status"] == "failed"


def test_hosts_file_is_only_bootstrap_input_not_runtime_permission(
    modules, fixture_root
):
    gpu_mcp_bootstrap, gpu_mcp_config, gpu_mcp_policy = modules
    hosts_file = fixture_root / "hosts.txt"
    hosts_file.write_text("gpu-a\ngpu-b\n")
    inventory_path = fixture_root / "bootstrap_hosts.json"

    gpu_mcp_bootstrap.bootstrap_hosts(
        hosts_file=hosts_file,
        inventory_path=inventory_path,
        ssh_probe={
            "gpu-a": {"status": "verified", "remote_hostname": "gpu-a"},
            "gpu-b": {"status": "verified", "remote_hostname": "gpu-b"},
        },
    )
    hosts_file.write_text("gpu-a\ngpu-b\ngpu-c\n")
    repo = fixture_root / "repo_a"
    config = _write_repo_policy(repo, nodes=["gpu-a"])
    policy = gpu_mcp_config.load_policy(config)

    with pytest.raises(gpu_mcp_policy.PolicyError, match="host not allowed"):
        gpu_mcp_policy.validate_host(policy, "gpu-b")
    with pytest.raises(gpu_mcp_policy.PolicyError, match="host not allowed"):
        gpu_mcp_policy.validate_host(policy, "gpu-c")


def test_bootstrap_inventory_is_evidence_not_server_policy(modules, fixture_root):
    gpu_mcp_bootstrap, gpu_mcp_config, gpu_mcp_policy = modules
    inventory_path = fixture_root / "bootstrap_hosts.json"
    gpu_mcp_bootstrap.bootstrap_hosts(
        hosts=["gpu-a", "gpu-b"],
        inventory_path=inventory_path,
        ssh_probe={
            "gpu-a": {"status": "verified", "remote_hostname": "gpu-a"},
            "gpu-b": {"status": "verified", "remote_hostname": "gpu-b"},
        },
    )
    repo = fixture_root / "repo_a"
    config = _write_repo_policy(repo, nodes=["gpu-a"])
    policy = gpu_mcp_config.load_policy(config)

    assert gpu_mcp_bootstrap.load_inventory(inventory_path)["hosts"][1]["host"] == "gpu-b"
    with pytest.raises(gpu_mcp_policy.PolicyError, match="host not allowed"):
        gpu_mcp_policy.validate_host(policy, "gpu-b")
