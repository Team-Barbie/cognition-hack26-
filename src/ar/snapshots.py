"""Content-addressed blobs, hashing, and path containment. Contracts owns the only blob writer."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_SHA256 = re.compile(r"[0-9a-f]{64}")


class BrokerError(Exception):
    def __init__(self, code: str, message: str = "", details: dict | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.details = details or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        if not path.exists():  # a concurrent identical writer winning is fine
            raise
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class BlobStore:
    """Immutable bytes keyed by sha256 under `<project>/.research/blobs`."""

    def __init__(self, project_root: Path) -> None:
        self.root = Path(project_root) / ".research" / "blobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        target = self.root / digest
        if not target.exists():
            atomic_write(target, data)
        return digest

    def get(self, digest: str) -> bytes:
        if not _SHA256.fullmatch(digest):
            raise BrokerError("SOURCE_NOT_FOUND", f"bad blob id {digest!r}")
        try:
            data = (self.root / digest).read_bytes()
        except FileNotFoundError:
            raise BrokerError("SOURCE_NOT_FOUND", f"blob {digest} is missing") from None
        if sha256_bytes(data) != digest:
            raise BrokerError("BLOB_CORRUPT", f"blob {digest} does not match its hash")
        return data


def resolve_inside(rel: str | Path, root: Path) -> Path:
    """Resolve `rel` under `root`; traversal, outside absolutes, and symlink escapes are denied."""
    try:
        base = Path(root).resolve()
        path = Path(rel)
        if ".." in path.parts:
            raise BrokerError("ACCESS_DENIED", f"path traversal in {str(rel)!r}")
        resolved = (path if path.is_absolute() else base / path).resolve()
    except (ValueError, OSError) as exc:
        raise BrokerError("ACCESS_DENIED", f"unusable path {str(rel)!r}: {exc}") from None
    if not resolved.is_relative_to(base):
        raise BrokerError("ACCESS_DENIED", f"{str(rel)!r} resolves outside {base}")
    return resolved
