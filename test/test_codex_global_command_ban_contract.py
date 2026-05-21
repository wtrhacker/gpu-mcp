from __future__ import annotations

"""Contract for v1 raw remote command prevention in Codex.

v1 does not implement a custom bypass-guard framework. GPU MCP installation
uses Codex execpolicy prompt rules for raw remote-access commands and verifies
that `codex exec` is running with approvals disabled, so prompt-required raw
remote commands are rejected before execution.

Expected future API:

- `gpu_mcp_codex_config.CodexConfigError`
- `gpu_mcp_codex_config.required_remote_command_prompt_rules() -> dict[str, str]`
- `gpu_mcp_codex_config.validate_remote_command_prompt_rules(rules_text) -> dict`
- `gpu_mcp_codex_config.build_remote_command_probe_argv(repo, command) -> list[str]`
- `gpu_mcp_codex_config.validate_remote_command_probe_argv(argv) -> dict`
- `gpu_mcp_codex_config.required_remote_command_ban_probes() -> tuple[str, ...]`
- `gpu_mcp_codex_config.evaluate_remote_command_ban_probe_results(results) -> dict`
"""

import importlib

import pytest


pytestmark = [
    pytest.mark.contract,
    pytest.mark.xfail(
        reason="Codex raw remote command ban validation is not implemented yet",
        strict=True,
    ),
]


@pytest.fixture()
def codex_config_module():
    return importlib.import_module("gpu_mcp_codex_config")


def _probe_result(command: str, *, blocked: bool, evidence: str | None = None) -> dict:
    return {
        "command": command,
        "blocked": blocked,
        "evidence": evidence or ("blocked by Codex policy" if blocked else "spawned"),
    }


def test_required_remote_command_ban_probes_are_simple_and_global(
    codex_config_module,
):
    probes = codex_config_module.required_remote_command_ban_probes()

    assert {"ssh", "scp", "sftp", "rsync"}.issubset(set(probes))
    assert all("gpu-a" not in probe for probe in probes)
    assert all("gpu-b" not in probe for probe in probes)


def test_required_remote_commands_use_codex_prompt_rules(codex_config_module):
    rules = codex_config_module.required_remote_command_prompt_rules()

    for command in ("ssh", "scp", "sftp", "rsync"):
        assert rules[command] == f'prefix_rule(pattern=["{command}"], decision="prompt")'


def test_global_prompt_rules_validate(codex_config_module):
    rules_text = "\n".join(
        [
            'prefix_rule(pattern=["ssh"], decision="prompt")',
            'prefix_rule(pattern=["scp"], decision="prompt")',
            'prefix_rule(pattern=["sftp"], decision="prompt")',
            'prefix_rule(pattern=["rsync"], decision="prompt")',
        ]
    )

    result = codex_config_module.validate_remote_command_prompt_rules(rules_text)

    assert result["status"] == "ok"
    assert result["scope"] == "global"
    assert set(result["prompt_commands"]) >= {"ssh", "scp", "sftp", "rsync"}


def test_allow_rules_do_not_count_as_raw_remote_blocks(codex_config_module):
    rules_text = "\n".join(
        [
            'prefix_rule(pattern=["ssh"], decision="allow")',
            'prefix_rule(pattern=["scp"], decision="prompt")',
            'prefix_rule(pattern=["sftp"], decision="prompt")',
            'prefix_rule(pattern=["rsync"], decision="prompt")',
        ]
    )

    with pytest.raises(codex_config_module.CodexConfigError, match="ssh"):
        codex_config_module.validate_remote_command_prompt_rules(rules_text)


def test_remote_command_probe_argv_uses_approval_never_without_ignore_rules(
    codex_config_module, tmp_path
):
    argv = codex_config_module.build_remote_command_probe_argv(tmp_path, "ssh")

    assert argv[:3] == ["codex", "--ask-for-approval", "never"]
    assert "exec" in argv
    assert "--ignore-rules" not in argv
    assert "ssh" in " ".join(argv)

    result = codex_config_module.validate_remote_command_probe_argv(argv)
    assert result["status"] == "ok"
    assert result["approval_policy"] == "never"
    assert result["rules_enabled"] is True


def test_remote_command_probe_argv_rejects_ignore_rules(codex_config_module, tmp_path):
    argv = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "-C",
        str(tmp_path),
        "--ignore-rules",
        "Run ssh.",
    ]

    with pytest.raises(codex_config_module.CodexConfigError, match="ignore-rules"):
        codex_config_module.validate_remote_command_probe_argv(argv)


def test_remote_command_probe_argv_rejects_approval_prompting(
    codex_config_module, tmp_path
):
    argv = [
        "codex",
        "--ask-for-approval",
        "on-request",
        "exec",
        "-C",
        str(tmp_path),
        "Run ssh.",
    ]

    with pytest.raises(codex_config_module.CodexConfigError, match="approval"):
        codex_config_module.validate_remote_command_probe_argv(argv)


def test_blocked_raw_remote_command_probes_validate(codex_config_module):
    results = [
        _probe_result("ssh", blocked=True),
        _probe_result("scp", blocked=True),
        _probe_result("sftp", blocked=True),
        _probe_result("rsync", blocked=True),
    ]

    result = codex_config_module.evaluate_remote_command_ban_probe_results(results)

    assert result["status"] == "ok"
    assert result["scope"] == "global"
    assert set(result["blocked_commands"]) >= {"ssh", "scp", "sftp", "rsync"}
    assert result["evidence_source"] == "codex_exec"


def test_missing_global_ssh_block_is_rejected(codex_config_module):
    results = [
        _probe_result("ssh", blocked=False, evidence="ssh attempted to spawn"),
        _probe_result("scp", blocked=True),
        _probe_result("sftp", blocked=True),
        _probe_result("rsync", blocked=True),
    ]

    with pytest.raises(codex_config_module.CodexConfigError, match="ssh"):
        codex_config_module.evaluate_remote_command_ban_probe_results(results)


def test_host_specific_remote_block_is_not_accepted_as_v1_policy(
    codex_config_module,
):
    results = [
        _probe_result("ssh gpu-a", blocked=True),
        _probe_result("scp gpu-a:/x .", blocked=True),
        _probe_result("sftp gpu-a", blocked=True),
        _probe_result("rsync gpu-a:/x .", blocked=True),
    ]

    with pytest.raises(codex_config_module.CodexConfigError, match="global"):
        codex_config_module.evaluate_remote_command_ban_probe_results(results)


def test_obvious_ssh_bypass_forms_are_reported(codex_config_module):
    results = [
        _probe_result("ssh", blocked=True),
        _probe_result("scp", blocked=True),
        _probe_result("sftp", blocked=True),
        _probe_result("rsync", blocked=True),
        _probe_result("/usr/bin/ssh", blocked=True),
        _probe_result("command ssh", blocked=True),
        _probe_result("env ssh", blocked=True),
    ]

    result = codex_config_module.evaluate_remote_command_ban_probe_results(results)

    assert result["status"] == "ok"
    assert "/usr/bin/ssh" in result["covered_bypass_forms"]
    assert "command ssh" in result["covered_bypass_forms"]
    assert "env ssh" in result["covered_bypass_forms"]
