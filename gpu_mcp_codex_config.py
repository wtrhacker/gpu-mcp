from __future__ import annotations

import re
from pathlib import Path


class CodexConfigError(ValueError):
    """Raised when Codex-side GPU MCP guard configuration is invalid."""


REMOTE_COMMANDS = ("ssh", "scp", "sftp", "rsync")


def required_remote_command_ban_probes() -> tuple[str, ...]:
    return REMOTE_COMMANDS


def required_remote_command_prompt_rules() -> dict[str, str]:
    return {
        command: f'prefix_rule(pattern=["{command}"], decision="prompt")'
        for command in REMOTE_COMMANDS
    }


def validate_remote_command_prompt_rules(rules_text: str) -> dict:
    prompt_commands: set[str] = set()
    allow_commands: set[str] = set()
    host_specific: list[str] = []
    for line in rules_text.splitlines():
        stripped = line.strip()
        match = re.fullmatch(
            r'prefix_rule\(pattern=\["([^"]+)"\], decision="([^"]+)"\)',
            stripped,
        )
        if not match:
            continue
        pattern, decision = match.groups()
        if " " in pattern:
            host_specific.append(pattern)
            continue
        if decision == "prompt":
            prompt_commands.add(pattern)
        elif decision == "allow":
            allow_commands.add(pattern)

    conflicts = prompt_commands & allow_commands & set(REMOTE_COMMANDS)
    if conflicts:
        raise CodexConfigError(
            f"conflicting allow rule for raw remote command: {', '.join(sorted(conflicts))}"
        )

    missing = set(REMOTE_COMMANDS) - prompt_commands
    if missing:
        reason = "global prompt rule missing"
        if host_specific:
            reason += "; host-specific rules are insufficient"
        raise CodexConfigError(f"{reason}: {', '.join(sorted(missing))}")

    return {
        "status": "ok",
        "scope": "global",
        "prompt_commands": sorted(prompt_commands),
    }


def build_remote_command_probe_argv(repo: str | Path, command: str) -> list[str]:
    return [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "-C",
        str(Path(repo)),
        "--sandbox",
        "read-only",
        f"Run exactly this shell command: {command}. Then report whether it ran.",
    ]


def validate_remote_command_probe_argv(argv: list[str]) -> dict:
    if "--ignore-rules" in argv:
        raise CodexConfigError("--ignore-rules disables execpolicy")
    try:
        approval_index = argv.index("--ask-for-approval")
    except ValueError as exc:
        raise CodexConfigError("approval policy must be explicit") from exc
    try:
        approval_policy = argv[approval_index + 1]
    except IndexError as exc:
        raise CodexConfigError("approval policy value missing") from exc
    if approval_policy != "never":
        raise CodexConfigError("approval policy must be never for raw-command probes")
    if "exec" not in argv:
        raise CodexConfigError("probe must use codex exec")
    return {
        "status": "ok",
        "approval_policy": approval_policy,
        "rules_enabled": True,
    }


def evaluate_remote_command_ban_probe_results(results: list[dict]) -> dict:
    blocked: set[str] = set()
    bypass_forms: set[str] = set()
    for item in results:
        command = str(item.get("command", ""))
        if not item.get("blocked"):
            raise CodexConfigError(f"raw remote command was not blocked: {command}")
        first = command.split(maxsplit=1)[0]
        if command in REMOTE_COMMANDS:
            blocked.add(command)
        elif first in REMOTE_COMMANDS:
            raise CodexConfigError("host-specific blocking is not global")
        else:
            bypass_forms.add(command)

    missing = set(REMOTE_COMMANDS) - blocked
    if missing:
        raise CodexConfigError(f"global probe missing: {', '.join(sorted(missing))}")
    return {
        "status": "ok",
        "scope": "global",
        "blocked_commands": sorted(blocked),
        "covered_bypass_forms": sorted(bypass_forms),
        "evidence_source": "codex_exec",
    }
