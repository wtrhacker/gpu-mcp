from __future__ import annotations

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


def _validate_roots(
    raw: object,
    *,
    name: str,
    repo_root: Path,
    allow_tmp: bool,
) -> tuple[Path, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{name} must be a list")
    roots = tuple(_resolve_root(repo_root, str(item)) for item in raw)
    for root in roots:
        if _inside(root, repo_root):
            continue
        if allow_tmp and _inside(root, Path("/tmp")):
            continue
        raise ConfigError(f"{name} root is outside repo_root: {root}")
    return roots


def load_policy(config_path: str | Path) -> GpuMcpPolicy:
    config = Path(config_path).expanduser()
    if not config.is_absolute():
        raise ConfigError("--config path must be absolute")
    config = config.resolve()
    if not config.exists():
        raise ConfigError(f"config file does not exist: {config}")

    with config.open("rb") as fh:
        raw = tomllib.load(fh)

    if raw.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")

    repo_root = Path(str(raw.get("repo_root", ""))).expanduser().resolve()
    if not repo_root:
        raise ConfigError("repo_root is required")
    if config.parent.resolve() != repo_root:
        raise ConfigError("repo_root must match the directory containing gpu-mcp.toml")

    nodes = tuple(str(node) for node in raw.get("nodes", []))
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

    return GpuMcpPolicy(
        config_path=config,
        repo_root=repo_root,
        nodes=nodes,
        script_roots=script_roots,
        write_roots=write_roots,
        output_roots=output_roots,
        allowed_gpu_names=tuple(str(name) for name in raw.get("allowed_gpu_names", [])),
        min_free_memory_mib=int(raw.get("min_free_memory_mib", 0)),
        sync_timeout_sec=int(raw.get("sync_timeout_sec", 30)),
    )
