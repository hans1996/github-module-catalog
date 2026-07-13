"""Linux descriptor-relative filesystem operations for catalog trust boundaries."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path, PurePosixPath


class UnsafeOutputPathError(ValueError):
    """Raised when catalog I/O encounters an unsafe filesystem object."""


FileIdentity = tuple[int, int, int]


def file_identity(details: os.stat_result) -> FileIdentity:
    """Return the device, inode, and file type used for race detection."""

    return details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode)


def open_directory(path: Path) -> int:
    """Open one trusted directory without following its final component."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise UnsafeOutputPathError("directory path is unsafe or missing") from None
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafeOutputPathError("directory path is not a directory")
    return descriptor


def simple_name(value: str, *, label: str) -> str:
    """Validate one descriptor-relative path component."""

    if value in {"", ".", ".."} or "/" in value or "\x00" in value:
        raise UnsafeOutputPathError(f"{label} must be a simple path component")
    return value


def relative_components(value: str | Path) -> tuple[str, ...]:
    """Parse a portable, non-escaping artifact path into safe components."""

    raw = value.as_posix() if isinstance(value, Path) else value
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or not candidate.parts:
        raise UnsafeOutputPathError("artifact path must remain inside the publication root")
    components = tuple(
        simple_name(component, label="artifact path component")
        for component in candidate.parts
    )
    if candidate.as_posix() != raw:
        raise UnsafeOutputPathError("artifact path is not canonical")
    return components


def stat_entry(parent_fd: int, name: str) -> os.stat_result | None:
    """Return a no-follow stat for one child, or None when absent."""

    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def require_identity(
    details: os.stat_result | None,
    expected: FileIdentity,
    *,
    message: str,
) -> os.stat_result:
    """Require an entry to remain the exact same inode and file type."""

    if details is None or file_identity(details) != expected:
        raise UnsafeOutputPathError(message)
    return details


def make_directory_at(parent_fd: int, name: str, *, mode: int = 0o700) -> tuple[int, FileIdentity]:
    """Create and open a new no-follow directory below an owned descriptor."""

    simple_name(name, label="directory name")
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError:
        raise UnsafeOutputPathError("directory could not be created safely") from None
    details = os.fstat(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise UnsafeOutputPathError("created object is not a directory")
    return descriptor, file_identity(details)


def write_regular_file_at(root_fd: int, relative_path: Path, content: bytes) -> None:
    """Create one immutable stage artifact through trusted directory descriptors."""

    components = relative_components(relative_path)
    directory_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            child_fd = _open_or_create_child(directory_fd, component)
            os.close(directory_fd)
            directory_fd = child_fd
        temporary = f".{components[-1]}.{uuid.uuid4().hex}"
        file_fd = -1
        try:
            file_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            _write_all(file_fd, content)
            os.fsync(file_fd)
            os.link(
                temporary,
                components[-1],
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
        except OSError:
            raise UnsafeOutputPathError("artifact could not be written safely") from None
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)


def read_regular_file_at(
    root_fd: int,
    relative_path: str | Path,
    *,
    max_bytes: int,
) -> bytes:
    """Open, bound-check, and read one regular file through one descriptor."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    components = relative_components(relative_path)
    directory_fd = os.dup(root_fd)
    file_fd = -1
    try:
        for component in components[:-1]:
            child_fd = _open_child_directory(directory_fd, component)
            os.close(directory_fd)
            directory_fd = child_fd
        try:
            file_fd = os.open(
                components[-1],
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
        except OSError:
            raise UnsafeOutputPathError("required catalog artifact is unsafe or missing") from None
        details = os.fstat(file_fd)
        if not stat.S_ISREG(details.st_mode) or details.st_size > max_bytes:
            raise UnsafeOutputPathError("catalog artifact is unsafe or exceeds the size limit")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(file_fd, min(1024 * 1024, max_bytes + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise UnsafeOutputPathError("catalog artifact exceeds the size limit")
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)


def list_regular_files_at(root_fd: int) -> set[str]:
    """List a tree while rejecting symlinks and non-regular leaf objects."""

    result: set[str] = set()
    _collect_regular_files(root_fd, (), result)
    return result


def remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    expected: FileIdentity | None = None,
) -> None:
    """Remove a directory tree without ever following a symbolic link."""

    simple_name(name, label="directory name")
    details = stat_entry(parent_fd, name)
    if details is None:
        return
    if expected is not None:
        require_identity(details, expected, message="directory changed during cleanup")
    if not stat.S_ISDIR(details.st_mode):
        raise UnsafeOutputPathError("cleanup target is not a directory")
    identity = file_identity(details)
    directory_fd = _open_child_directory(parent_fd, name)
    try:
        if file_identity(os.fstat(directory_fd)) != identity:
            raise UnsafeOutputPathError("directory changed during cleanup")
        for child in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, child)
    finally:
        os.close(directory_fd)
    require_identity(
        stat_entry(parent_fd, name),
        identity,
        message="directory changed during cleanup",
    )
    os.rmdir(name, dir_fd=parent_fd)


def _open_or_create_child(parent_fd: int, name: str) -> int:
    try:
        return _open_child_directory(parent_fd, name)
    except UnsafeOutputPathError:
        if stat_entry(parent_fd, name) is not None:
            raise
    child_fd, _identity = make_directory_at(parent_fd, name)
    return child_fd


def _open_child_directory(parent_fd: int, name: str) -> int:
    simple_name(name, label="directory name")
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError:
        raise UnsafeOutputPathError("catalog directory component is unsafe") from None
    if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
        os.close(child_fd)
        raise UnsafeOutputPathError("catalog directory component is unsafe")
    return child_fd


def _write_all(file_fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError("short artifact write")
        view = view[written:]


def _collect_regular_files(
    directory_fd: int, prefix: tuple[str, ...], result: set[str]
) -> None:
    for name in os.listdir(directory_fd):
        simple_name(name, label="catalog entry")
        details = stat_entry(directory_fd, name)
        if details is None:
            raise UnsafeOutputPathError("catalog output changed during validation")
        relative = (*prefix, name)
        if stat.S_ISREG(details.st_mode):
            result.add("/".join(relative))
        elif stat.S_ISDIR(details.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                if file_identity(os.fstat(child_fd)) != file_identity(details):
                    raise UnsafeOutputPathError("catalog output changed during validation")
                _collect_regular_files(child_fd, relative, result)
            finally:
                os.close(child_fd)
        else:
            raise UnsafeOutputPathError("catalog output contains an unsafe filesystem object")


def _remove_entry_at(parent_fd: int, name: str) -> None:
    details = stat_entry(parent_fd, name)
    if details is None:
        return
    if stat.S_ISDIR(details.st_mode):
        remove_tree_at(parent_fd, name, expected=file_identity(details))
        return
    identity = file_identity(details)
    require_identity(
        stat_entry(parent_fd, name),
        identity,
        message="entry changed during cleanup",
    )
    os.unlink(name, dir_fd=parent_fd)
