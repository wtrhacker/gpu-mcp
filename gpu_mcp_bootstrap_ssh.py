#!/usr/bin/env python3
"""Bootstrap the dedicated GPU MCP SSH key onto cluster hosts.

This mirrors the saunasub setup model: authenticate once with the user's normal
SSH password, install a fresh dedicated public key on each destination host, and
then verify passwordless login with that dedicated key.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import paramiko

logging.getLogger("paramiko").setLevel(logging.WARNING)


def default_hosts() -> list[str]:
    """Use the same host list as the MCP server."""
    from gpu_mcp_server import NODES

    hosts = []
    for node in NODES:
        host = node.split("@")[-1].strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def run_checked(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def key_fingerprint(pub_path: Path) -> str:
    result = run_checked(["ssh-keygen", "-lf", str(pub_path)])
    return result.stdout.strip()


def ensure_key(key_path: Path) -> Path:
    key_path = key_path.expanduser()
    pub_path = Path(f"{key_path}.pub")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.chmod(0o700)

    if not key_path.exists():
        run_checked(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-f",
                str(key_path),
                "-N",
                "",
                "-C",
                f"gpu-mcp-{socket.gethostname()}",
            ]
        )
    if not pub_path.exists():
        pub = run_checked(["ssh-keygen", "-y", "-f", str(key_path)]).stdout
        pub_path.write_text(pub)

    key_path.chmod(0o600)
    pub_path.chmod(0o644)
    return pub_path


def remote_exec(client: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command)
    del stdin
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), out, err


def install_key_with_password(host: str, user: str, password: str, public_key: str) -> None:
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
        status, out, err = remote_exec(client, setup_cmd)
        if status != 0:
            raise RuntimeError(f"remote ssh setup failed: {out}{err}")

        quoted_key = shlex.quote(public_key)
        install_cmd = (
            "grep -qxF -- "
            f"{quoted_key} ~/.ssh/authorized_keys "
            f"|| printf '%s\\n' {quoted_key} >> ~/.ssh/authorized_keys"
        )
        status, out, err = remote_exec(client, install_cmd)
        if status != 0:
            raise RuntimeError(f"remote key install failed: {out}{err}")
    finally:
        client.close()


def verify_key_login(host: str, user: str, key_path: Path) -> str:
    result = subprocess.run(
        [
            "ssh",
            "-i",
            str(key_path),
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
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install and verify the dedicated GPU MCP SSH key."
    )
    parser.add_argument(
        "hosts",
        nargs="*",
        help="Target hosts. If omitted, all hosts from gpu_mcp_server.NODES are used.",
    )
    parser.add_argument("--user", default=os.environ.get("GPU_MCP_USER") or getpass.getuser())
    parser.add_argument(
        "--key",
        default=os.environ.get("GPU_MCP_SSH_KEY") or str(Path.home() / ".ssh" / "gpu_mcp_key"),
    )
    parser.add_argument(
        "--list-hosts",
        action="store_true",
        help="Print the default host list and exit.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not prompt for a password or install keys; only verify existing dedicated-key login.",
    )
    args = parser.parse_args()
    hosts = args.hosts or default_hosts()

    if args.list_hosts:
        print("\n".join(hosts))
        return 0

    key_path = Path(args.key).expanduser()
    if args.verify_only:
        pub_path = Path(f"{key_path}.pub")
        if not key_path.exists() or not pub_path.exists():
            print(
                f"ERROR: missing dedicated key or public key: {key_path} / {pub_path}",
                file=sys.stderr,
            )
            return 2
    else:
        pub_path = ensure_key(key_path)
    public_key = pub_path.read_text().strip()

    print("Using GPU MCP public key:")
    print(key_fingerprint(pub_path), flush=True)
    print()

    password = None
    if not args.verify_only:
        password = getpass.getpass(f"SSH password for {args.user} on target hosts: ")

    failures: list[tuple[str, str]] = []
    for host in hosts:
        action = "Verifying key on" if args.verify_only else "Installing key on"
        print(f"==> {action} {args.user}@{host}")
        try:
            if password is not None:
                install_key_with_password(host, args.user, password, public_key)
            remote_name = verify_key_login(host, args.user, key_path)
            print(f"    verified dedicated-key login: {remote_name}")
        except Exception as exc:
            failures.append((host, str(exc)))
            print(f"    FAILED: {exc}")

    if failures:
        print("\nFailures:")
        for host, error in failures:
            print(f"- {host}: {error}")
        return 1

    print("\nGPU MCP SSH bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
