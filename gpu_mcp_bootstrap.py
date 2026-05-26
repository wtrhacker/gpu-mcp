from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)


class BootstrapError(ValueError):
    """Raised when human-first SSH bootstrap input is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_hosts_file(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _run_checked(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def ensure_key(key_path: str | Path) -> Path:
    """Create the dedicated GPU MCP SSH key if it does not already exist."""
    private_key = Path(key_path).expanduser()
    public_key = Path(f"{private_key}.pub")
    if private_key.is_symlink() or public_key.is_symlink():
        raise BootstrapError("dedicated key path must not be a symlink")
    private_key.parent.mkdir(parents=True, exist_ok=True)
    private_key.parent.chmod(0o700)

    if not private_key.exists():
        _run_checked(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(private_key),
                "-N",
                "",
                "-C",
                f"gpu-mcp-{socket.gethostname()}",
            ]
        )
    if not public_key.exists():
        public_key.write_text(_run_checked(["ssh-keygen", "-y", "-f", str(private_key)]).stdout)

    private_key.chmod(0o600)
    public_key.chmod(0o644)
    return public_key


def key_fingerprint(public_key_path: str | Path) -> str:
    return _run_checked(["ssh-keygen", "-lf", str(Path(public_key_path).expanduser())]).stdout.strip()


def _remote_exec(client: Any, command: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command)
    del stdin
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def install_key_with_password(host: str, user: str, password: str, public_key: str) -> None:
    """Install the dedicated public key after the human enters their SSH password."""
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise BootstrapError("paramiko is required for --install") from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            username=user,
            password=password,
            timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
        setup_cmd = (
            "umask 077; "
            "mkdir -p ~/.ssh; "
            "touch ~/.ssh/authorized_keys; "
            "chmod 700 ~/.ssh; "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        status, out, err = _remote_exec(client, setup_cmd)
        if status != 0:
            raise BootstrapError(f"remote ssh setup failed: {out}{err}")

        quoted_key = shlex.quote(public_key)
        install_cmd = (
            "grep -qxF -- "
            f"{quoted_key} ~/.ssh/authorized_keys "
            f"|| printf '%s\\n' {quoted_key} >> ~/.ssh/authorized_keys"
        )
        status, out, err = _remote_exec(client, install_cmd)
        if status != 0:
            raise BootstrapError(f"remote key install failed: {out}{err}")
    finally:
        client.close()


def verify_key_login(host: str, user: str, key_path: str | Path, timeout: int = 20) -> str:
    """Verify passwordless login using only the dedicated GPU MCP key."""
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(Path(key_path).expanduser()),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            f"{user}@{host}",
            "hostname",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise BootstrapError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def _probe_hosts(
    hosts: list[str],
    *,
    ssh_user: str,
    key_path: Path,
    install_key: bool,
    password: str | None,
    verify_timeout: int,
) -> dict[str, dict[str, Any]]:
    public_key_path = ensure_key(key_path) if install_key else Path(f"{key_path}.pub").expanduser()
    if not key_path.exists() or not public_key_path.exists():
        raise BootstrapError(
            f"missing dedicated key or public key: {key_path} / {public_key_path}; "
            "rerun with --install for first-time setup"
        )
    public_key = public_key_path.read_text().strip()
    results: dict[str, dict[str, Any]] = {}

    for host in hosts:
        action = "Installing and verifying" if install_key else "Verifying"
        print(f"==> {action} {ssh_user}@{host}", flush=True)
        try:
            if install_key:
                if password is None:
                    raise BootstrapError("password is required when install_key=True")
                install_key_with_password(host, ssh_user, password, public_key)
            remote_hostname = verify_key_login(host, ssh_user, key_path, timeout=verify_timeout)
            results[host] = {
                "status": "verified",
                "remote_hostname": remote_hostname,
                "host_key_recorded": True,
                "host_key_policy": "accept-new",
                "verified_at": _now(),
            }
            print(f"    verified dedicated-key login: {remote_hostname}", flush=True)
        except Exception as exc:
            results[host] = {
                "status": "failed",
                "error": str(exc),
                "host_key_recorded": False,
                "host_key_policy": "not-recorded",
                "verified_at": _now(),
            }
            print(f"    FAILED: {exc}", flush=True)
    return results


def bootstrap_hosts(
    *,
    hosts: list[str] | None = None,
    hosts_file: str | Path | None = None,
    inventory_path: str | Path,
    ssh_probe: dict[str, dict[str, Any]] | None = None,
    ssh_user: str | None = None,
    key_path: str | Path = "~/.ssh/gpu_mcp_key",
    key_fingerprint: str | None = None,
    install_key: bool = False,
    password: str | None = None,
    verify_timeout: int = 20,
) -> dict:
    if hosts and hosts_file:
        raise BootstrapError("choose hosts or hosts_file, not both")
    if not hosts and not hosts_file:
        raise BootstrapError("explicit hosts or hosts_file is required")

    selected = list(hosts or _read_hosts_file(Path(hosts_file).expanduser()))
    if not selected:
        raise BootstrapError("hosts list is empty")

    user = ssh_user or getpass.getuser()
    key = Path(key_path).expanduser()
    probe = ssh_probe
    if probe is None:
        if install_key and password is None:
            password = getpass.getpass(f"SSH password for {user} on target hosts: ")
        probe = _probe_hosts(
            selected,
            ssh_user=user,
            key_path=key,
            install_key=install_key,
            password=password,
            verify_timeout=verify_timeout,
        )
    public_key_path = Path(f"{key}.pub")
    fingerprint = key_fingerprint
    if fingerprint is None:
        fingerprint = key_fingerprint_from_file(public_key_path) if public_key_path.exists() else "unprobed"
    generated_at = _now()
    inventory = {
        "schema_version": 1,
        "generated_at": generated_at,
        "generated_by": "gpu_mcp_bootstrap.py",
        "ssh_user": user,
        "key_path": str(key),
        "key_fingerprint": fingerprint,
        "hosts": [],
    }

    for host in selected:
        result = probe.get(host, {"status": "failed", "error": "not probed"})
        status = result.get("status", "failed")
        inventory["hosts"].append(
            {
                "host": host,
                "status": status,
                "remote_hostname": result.get("remote_hostname", "") if status == "verified" else "",
                "host_key_recorded": bool(result.get("host_key_recorded", status == "verified")),
                "host_key_policy": str(
                    result.get(
                        "host_key_policy",
                        "accept-new" if status == "verified" else "not-recorded",
                    )
                ),
                "verified_at": result.get("verified_at", generated_at),
                "error": result.get("error", "") if status != "verified" else "",
            }
        )

    output = Path(inventory_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inventory, indent=2) + "\n")
    return inventory


def load_inventory(path: str | Path) -> dict:
    return json.loads(Path(path).expanduser().read_text())


def key_fingerprint_from_file(public_key_path: str | Path) -> str:
    return key_fingerprint(public_key_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap and verify dedicated SSH access for GPU MCP hosts."
    )
    parser.add_argument("hosts", nargs="*", help="Explicit target hosts to bootstrap.")
    parser.add_argument("--hosts-file", help="File containing one host per line.")
    parser.add_argument(
        "--inventory",
        default=str(Path.home() / ".cache" / "gpu-mcp" / "bootstrap_hosts.json"),
        help="Path to write bootstrap inventory JSON.",
    )
    parser.add_argument("--user", default=os.environ.get("GPU_MCP_USER") or getpass.getuser())
    parser.add_argument(
        "--key",
        default=os.environ.get("GPU_MCP_SSH_KEY") or str(Path.home() / ".ssh" / "gpu_mcp_key"),
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Prompt for the human SSH password and install the dedicated public key.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=20,
        help="Per-host dedicated-key verification timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        inventory = bootstrap_hosts(
            hosts=args.hosts or None,
            hosts_file=args.hosts_file,
            inventory_path=args.inventory,
            ssh_user=args.user,
            key_path=args.key,
            install_key=args.install,
            verify_timeout=args.verify_timeout,
        )
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for item in inventory["hosts"]:
        if item["status"] == "verified":
            print(f"verified {args.user}@{item['host']}: {item['remote_hostname']}")
        else:
            print(f"failed {args.user}@{item['host']}: {item['error']}")
    print(f"wrote {args.inventory}")
    return 0 if all(item["status"] == "verified" for item in inventory["hosts"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
