"""Inspect the existing Codex daemon without owning its package lifecycle."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess


class CodexVersionError(RuntimeError):
    pass


def inspect_versions(*, cli=None, runner=subprocess.run):
    cli = cli or shutil.which("codex")
    if not cli:
        raise CodexVersionError("Codex CLI is unavailable")
    response = runner([cli, "app-server", "daemon", "version"], capture_output=True, text=True, timeout=15)
    if response.returncode:
        raise CodexVersionError("Cannot inspect the existing Codex daemon: " + response.stderr.strip())
    info = json.loads(response.stdout)
    info["cli_path"] = cli
    return info


def require_daemon(*, cli=None, runner=subprocess.run):
    info = inspect_versions(cli=cli, runner=runner)
    if info.get("status") != "running" or not info.get("appServerVersion"):
        raise CodexVersionError("The Codex daemon is unavailable. Start Codex normally and retry. "
            "GPU MCP does not install, pin, update or restart the shared daemon.")
    # Native initialize and the actual RPC responses enforce wire compatibility.
    # An independent latest-channel daemon can differ from a client's version.
    # Equality would turn every ordinary CLI update into an MCP outage.
    return info


def require_match(*, cli=None, runner=subprocess.run):
    """Compatibility name for older clients; version equality is no longer a gate."""
    return require_daemon(cli=cli, runner=runner)


def pin_current_cli(**kwargs):
    """Refuse the retired helper before any global mutation."""
    raise CodexVersionError("GPU MCP daemon pinning has been retired: --from-cli freezes the selected "
        "package and requires manual repair after CLI upgrades. Use Codex's native production "
        "daemon updater for installation maintenance; GPU MCP leaves it independent.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli")
    parser.add_argument("--pin-current-cli", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = pin_current_cli(cli=args.cli) if args.pin_current_cli else require_daemon(cli=args.cli)
        print(result if isinstance(result, str) else json.dumps(result))
    except (CodexVersionError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
