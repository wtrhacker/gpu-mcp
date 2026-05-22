from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class PolicyMutationError(AssertionError):
    """Raised when a protected probe mutates a policy file."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    digest: str


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_file(path: str | Path) -> FileSnapshot:
    resolved = Path(path).resolve()
    return FileSnapshot(path=resolved, digest=_digest(resolved))


def assert_file_unchanged(snapshot: FileSnapshot) -> None:
    current = _digest(snapshot.path)
    if current != snapshot.digest:
        raise PolicyMutationError(f"{snapshot.path.name} changed during protected probe")
