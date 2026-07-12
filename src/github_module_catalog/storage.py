"""Immutable content-addressed storage for raw GitHub response bodies."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class InvalidDigestError(ValueError):
    """Raised when a digest cannot safely address a raw object."""


class DigestMismatchError(ValueError):
    """Raised when supplied bytes do not match their declared digest."""


class ObjectCollisionError(RuntimeError):
    """Raised when an immutable object path already contains different bytes."""


@dataclass(frozen=True, slots=True)
class RawObject:
    """A verified immutable raw object."""

    sha256: str
    path: Path
    size_bytes: int


class RawObjectStore:
    """Write exact response bodies to deterministic SHA-256 paths."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._object_root = self._workspace_root / "data" / "raw" / "sha256"

    @property
    def workspace_root(self) -> Path:
        """Return the configured workspace root."""

        return self._workspace_root

    def path_for(self, sha256: str) -> Path:
        """Return the only valid path for a lowercase SHA-256 digest."""

        _validate_digest(sha256)
        target = self._object_root / sha256[:2] / f"{sha256}.json"
        resolved_root = self._object_root.resolve()
        if not resolved_root.is_relative_to(self._workspace_root):
            raise InvalidDigestError("raw object root resolves outside the workspace")
        if not target.resolve().is_relative_to(resolved_root):
            raise InvalidDigestError("digest resolves outside the raw object store")
        return target

    def write(self, raw_bytes: bytes, *, expected_sha256: str | None = None) -> RawObject:
        """Atomically persist bytes or return the identical existing object."""

        if not isinstance(raw_bytes, bytes):
            raise TypeError("raw_bytes must be bytes")
        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if expected_sha256 is not None:
            _validate_digest(expected_sha256)
            if expected_sha256 != actual_sha256:
                raise DigestMismatchError("raw bytes do not match expected SHA-256")
        target = self.path_for(actual_sha256)
        existing = self._verify_existing(target, actual_sha256, raw_bytes)
        if existing is not None:
            return existing

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{actual_sha256}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(raw_bytes)
                temporary.flush()
                os.fsync(temporary.fileno())

            existing = self._verify_existing(target, actual_sha256, raw_bytes)
            if existing is not None:
                return existing
            os.replace(temporary_path, target)
            temporary_path = None
            _fsync_directory(target.parent)
            return RawObject(actual_sha256, target, len(raw_bytes))
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def verify(self, sha256: str, *, expected_bytes: bytes | None = None) -> RawObject:
        """Verify a referenced object without exposing mutable store state."""

        target = self.path_for(sha256)
        if not target.is_file():
            raise FileNotFoundError(f"raw object does not exist: {sha256}")
        observed = target.read_bytes()
        if hashlib.sha256(observed).hexdigest() != sha256:
            raise ObjectCollisionError(f"raw object failed digest verification: {sha256}")
        if expected_bytes is not None and observed != expected_bytes:
            raise ObjectCollisionError(f"raw object bytes differ from expected content: {sha256}")
        return RawObject(sha256, target, len(observed))

    def read(self, sha256: str) -> bytes:
        """Return an immutable copy of a verified object's bytes."""

        verified = self.verify(sha256)
        return verified.path.read_bytes()

    @staticmethod
    def _verify_existing(target: Path, sha256: str, raw_bytes: bytes) -> RawObject | None:
        if not target.exists():
            return None
        if not target.is_file():
            raise ObjectCollisionError(f"raw object path is not a regular file: {sha256}")
        observed = target.read_bytes()
        if hashlib.sha256(observed).hexdigest() != sha256 or observed != raw_bytes:
            raise ObjectCollisionError(f"immutable raw object collision: {sha256}")
        return RawObject(sha256, target, len(observed))


def _validate_digest(sha256: str) -> None:
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        raise InvalidDigestError("SHA-256 must be exactly 64 lowercase hexadecimal characters")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
