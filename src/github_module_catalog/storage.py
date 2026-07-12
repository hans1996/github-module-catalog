"""Immutable content-addressed storage for raw GitHub response bodies."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
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
        target.parent.mkdir(parents=True, exist_ok=True)
        directory_fd = _open_directory(target.parent)
        temporary_name: str | None = None
        try:
            temporary_name, temporary_fd = _create_temporary(directory_fd, actual_sha256)
            try:
                _write_all(temporary_fd, raw_bytes)
                os.fsync(temporary_fd)
            finally:
                os.close(temporary_fd)

            try:
                os.link(
                    temporary_name,
                    target.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            else:
                os.fsync(directory_fd)

            observed = _read_verified_at(
                directory_fd, target.name, actual_sha256, expected_bytes=raw_bytes
            )
            return RawObject(actual_sha256, target, len(observed))
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)

    def verify(self, sha256: str, *, expected_bytes: bytes | None = None) -> RawObject:
        """Verify a referenced object without exposing mutable store state."""

        target = self.path_for(sha256)
        directory_fd = _open_directory(target.parent)
        try:
            observed = _read_verified_at(
                directory_fd, target.name, sha256, expected_bytes=expected_bytes
            )
            return RawObject(sha256, target, len(observed))
        finally:
            os.close(directory_fd)

    def read(self, sha256: str) -> bytes:
        """Return an immutable copy of a verified object's bytes."""

        target = self.path_for(sha256)
        directory_fd = _open_directory(target.parent)
        try:
            return _read_verified_at(directory_fd, target.name, sha256)
        finally:
            os.close(directory_fd)


def _validate_digest(sha256: str) -> None:
    if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
        raise InvalidDigestError("SHA-256 must be exactly 64 lowercase hexadecimal characters")


def _open_directory(directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(directory, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ObjectCollisionError("raw object directory is not a safe directory") from error


def _create_temporary(directory_fd: int, sha256: str) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(10):
        name = f".{sha256}.{secrets.token_hex(16)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
    raise FileExistsError("could not allocate an exclusive raw object temporary file")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        offset += os.write(descriptor, content[offset:])


def _read_verified_at(
    directory_fd: int,
    name: str,
    sha256: str,
    *,
    expected_bytes: bytes | None = None,
) -> bytes:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        raise FileNotFoundError(f"raw object does not exist: {sha256}") from None
    except OSError as error:
        raise ObjectCollisionError(f"raw object is not a safe regular file: {sha256}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ObjectCollisionError(f"raw object is not a regular file: {sha256}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    observed = b"".join(chunks)
    if hashlib.sha256(observed).hexdigest() != sha256:
        raise ObjectCollisionError(f"raw object failed digest verification: {sha256}")
    if expected_bytes is not None and observed != expected_bytes:
        raise ObjectCollisionError(f"raw object bytes differ from expected content: {sha256}")
    return observed
