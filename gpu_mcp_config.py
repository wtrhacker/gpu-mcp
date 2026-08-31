from __future__ import annotations

import hashlib
import io
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when a repo-local GPU MCP policy file is invalid."""


@dataclass(frozen=True)
class GpuMcpPolicy:
    config_path: Path
    repo_root: Path
    nodes: tuple[str, ...]
    script_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    output_roots: tuple[Path, ...]
    allowed_gpu_names: tuple[str, ...]
    min_free_memory_mib: int
    sync_timeout_sec: int


@dataclass(frozen=True)
class PolicySnapshot:
    """A policy parsed and hashed from one immutable byte snapshot."""

    policy: GpuMcpPolicy
    content_hash: str


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_root(repo_root: Path, item: str) -> Path:
    path = Path(item).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _string_list(raw: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{name} must be a list")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} entries must be non-empty strings")
        values.append(item)
    return tuple(values)


def _non_negative_int(raw: object, *, name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{name} must be an integer")
    if raw < 0:
        raise ConfigError(f"{name} must be non-negative")
    return raw


def _positive_int(raw: object, *, name: str) -> int:
    value = _non_negative_int(raw, name=name)
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _validate_roots(
    raw: object,
    *,
    name: str,
    repo_root: Path,
    allow_tmp: bool,
) -> tuple[Path, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{name} must be a list")
    root_items = _string_list(raw, name=name)
    roots = tuple(_resolve_root(repo_root, item) for item in root_items)
    for root in roots:
        if _inside(root, repo_root):
            continue
        if allow_tmp and _inside(root, Path("/tmp")):
            continue
        raise ConfigError(f"{name} root is outside repo_root: {root}")
    return roots


def load_policy_snapshot(config_path: str | Path) -> PolicySnapshot:
    """Read, validate, and hash a policy without a parse/hash race."""
    config = Path(config_path).expanduser()
    if not config.is_absolute():
        raise ConfigError("--config path must be absolute")
    if config.is_symlink():
        raise ConfigError("gpu-mcp.toml must not be a symlink")
    config = config.resolve()
    if not config.exists():
        raise ConfigError(f"config file does not exist: {config}")

    try:
        contents = config.read_bytes()
    except OSError as exc:
        raise ConfigError(f"could not read config file: {config}: {exc}") from exc
    try:
        raw = tomllib.load(io.BytesIO(contents))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file is invalid TOML: {exc}") from exc

    if raw.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")

    repo_root_raw = raw.get("repo_root")
    if not isinstance(repo_root_raw, str) or not repo_root_raw.strip():
        raise ConfigError("repo_root is required")
    repo_root = Path(repo_root_raw).expanduser().resolve()
    if config.parent.resolve() != repo_root:
        raise ConfigError("repo_root must match the directory containing gpu-mcp.toml")

    nodes = _string_list(raw.get("nodes", []), name="nodes")
    if not nodes:
        raise ConfigError("nodes must contain at least one host")

    script_roots = _validate_roots(
        raw.get("script_roots", []),
        name="script_roots",
        repo_root=repo_root,
        allow_tmp=False,
    )
    write_roots = _validate_roots(
        raw.get("write_roots", []),
        name="write_roots",
        repo_root=repo_root,
        allow_tmp=True,
    )
    output_roots = _validate_roots(
        raw.get("output_roots", []),
        name="output_roots",
        repo_root=repo_root,
        allow_tmp=True,
    )
    if not script_roots:
        raise ConfigError("script_roots must not be empty")
    if not write_roots:
        raise ConfigError("write_roots must not be empty")
    if not output_roots:
        raise ConfigError("output_roots must not be empty")

    policy = GpuMcpPolicy(
        config_path=config,
        repo_root=repo_root,
        nodes=nodes,
        script_roots=script_roots,
        write_roots=write_roots,
        output_roots=output_roots,
        allowed_gpu_names=_string_list(raw.get("allowed_gpu_names", []), name="allowed_gpu_names"),
        min_free_memory_mib=_non_negative_int(
            raw.get("min_free_memory_mib", 0),
            name="min_free_memory_mib",
        ),
        sync_timeout_sec=_positive_int(
            raw.get("sync_timeout_sec", 30),
            name="sync_timeout_sec",
        ),
    )
    return PolicySnapshot(
        policy=policy,
        content_hash=hashlib.sha256(contents).hexdigest(),
    )


def load_policy(config_path: str | Path) -> GpuMcpPolicy:
    return load_policy_snapshot(config_path).policy
