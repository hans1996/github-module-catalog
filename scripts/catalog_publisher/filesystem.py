"""Transactional promotion and descriptor-relative safe filesystem operations."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from .constants import MAX_ARTIFACTS, MAX_FILE_BYTES, MAX_README_BYTES
from .model import FileIdentity, NodeIdentity, PublicationError


def _promote_transaction(
    repository_fd: int,
    *,
    stage_name: str,
    readme_temp: str,
    catalog_backup: str,
    readme_backup: str,
    stage_identity: NodeIdentity,
    readme_temp_identity: FileIdentity,
    catalog_identity: NodeIdentity | None,
    readme_identity: FileIdentity,
    expected_catalog: Mapping[str, bytes],
    expected_readme_before: bytes,
    expected_readme_after: bytes,
) -> None:
    catalog_backup_identity: NodeIdentity | None = None
    readme_backup_identity: FileIdentity | None = None
    catalog_installed = False
    readme_installed = False
    _verify_regular_contents_at(
        repository_fd,
        "README.md",
        expected_content=expected_readme_before,
        expected_file_identity=readme_identity,
    )
    if catalog_identity is None:
        _require_missing(repository_fd, "catalog")
    else:
        _require_node_identity_at(repository_fd, "catalog", catalog_identity)
    _verify_catalog_stage(
        repository_fd,
        stage_name,
        expected_identity=stage_identity,
        expected_contents=expected_catalog,
    )
    _verify_regular_contents_at(
        repository_fd,
        readme_temp,
        expected_content=expected_readme_after,
        expected_file_identity=readme_temp_identity,
    )
    try:
        _write_regular_at(repository_fd, readme_backup, expected_readme_before)
        _, readme_backup_identity = _read_regular_at(
            repository_fd,
            readme_backup,
            max_bytes=MAX_README_BYTES,
        )
        _verify_regular_contents_at(
            repository_fd,
            readme_backup,
            expected_content=expected_readme_before,
            expected_file_identity=readme_backup_identity,
        )
        os.fsync(repository_fd)
        if catalog_identity is not None:
            os.rename(
                "catalog",
                catalog_backup,
                src_dir_fd=repository_fd,
                dst_dir_fd=repository_fd,
            )
            catalog_backup_identity = catalog_identity
            _require_node_identity_at(repository_fd, catalog_backup, catalog_backup_identity)
            os.fsync(repository_fd)
        _verify_catalog_stage(
            repository_fd,
            stage_name,
            expected_identity=stage_identity,
            expected_contents=expected_catalog,
        )
        _verify_regular_contents_at(
            repository_fd,
            "README.md",
            expected_content=expected_readme_before,
        )
        _verify_regular_contents_at(
            repository_fd,
            readme_temp,
            expected_content=expected_readme_after,
            expected_file_identity=readme_temp_identity,
        )
        os.rename(
            stage_name,
            "catalog",
            src_dir_fd=repository_fd,
            dst_dir_fd=repository_fd,
        )
        catalog_installed = True
        _require_node_identity_at(repository_fd, "catalog", stage_identity)
        os.replace(
            readme_temp,
            "README.md",
            src_dir_fd=repository_fd,
            dst_dir_fd=repository_fd,
        )
        readme_installed = True
        _verify_regular_contents_at(
            repository_fd,
            "README.md",
            expected_content=expected_readme_after,
            expected_node_identity=readme_temp_identity[:3],
        )
        os.fsync(repository_fd)
    except BaseException as original_error:
        rollback_errors = _rollback_transaction(
            repository_fd,
            stage_name=stage_name,
            stage_identity=stage_identity,
            readme_temp_identity=readme_temp_identity,
            catalog_backup=catalog_backup,
            catalog_backup_identity=catalog_backup_identity,
            readme_backup=readme_backup,
            readme_backup_identity=readme_backup_identity,
            catalog_installed=catalog_installed,
            readme_installed=readme_installed,
            expected_readme_before=expected_readme_before,
        )
        for rollback_error in rollback_errors:
            original_error.add_note(f"rollback also failed: {rollback_error}")
        raise
    if catalog_backup_identity is not None:
        _cleanup_owned_if_present(repository_fd, catalog_backup, catalog_backup_identity)
    if readme_backup_identity is not None:
        _cleanup_owned_if_present(repository_fd, readme_backup, readme_backup_identity)
    os.fsync(repository_fd)


def _rollback_transaction(
    repository_fd: int,
    *,
    stage_name: str,
    stage_identity: NodeIdentity,
    readme_temp_identity: FileIdentity,
    catalog_backup: str,
    catalog_backup_identity: NodeIdentity | None,
    readme_backup: str,
    readme_backup_identity: FileIdentity | None,
    catalog_installed: bool,
    readme_installed: bool,
    expected_readme_before: bytes,
) -> list[BaseException]:
    errors: list[BaseException] = []
    operations: tuple[Callable[[], None], ...] = (
        lambda: _rollback_readme(
            repository_fd,
            readme_temp_identity=readme_temp_identity,
            readme_backup=readme_backup,
            readme_backup_identity=readme_backup_identity,
            readme_installed=readme_installed,
            expected_readme_before=expected_readme_before,
        ),
        lambda: _rollback_catalog(
            repository_fd,
            stage_name=stage_name,
            stage_identity=stage_identity,
            catalog_backup=catalog_backup,
            catalog_backup_identity=catalog_backup_identity,
            catalog_installed=catalog_installed,
        ),
        lambda: os.fsync(repository_fd),
    )
    for operation in operations:
        try:
            operation()
        except BaseException as error:
            errors.append(error)
    return errors


def _rollback_readme(
    repository_fd: int,
    *,
    readme_temp_identity: FileIdentity,
    readme_backup: str,
    readme_backup_identity: FileIdentity | None,
    readme_installed: bool,
    expected_readme_before: bytes,
) -> None:
    if readme_backup_identity is None:
        return
    _verify_regular_contents_at(
        repository_fd,
        readme_backup,
        expected_content=expected_readme_before,
        expected_file_identity=readme_backup_identity,
    )
    observed_readme = _stat_at(repository_fd, "README.md")
    if readme_installed:
        if observed_readme is not None:
            _require_node_identity_at(
                repository_fd,
                "README.md",
                readme_temp_identity[:3],
            )
        os.replace(
            readme_backup,
            "README.md",
            src_dir_fd=repository_fd,
            dst_dir_fd=repository_fd,
        )
        _verify_regular_contents_at(
            repository_fd,
            "README.md",
            expected_content=expected_readme_before,
        )
        return
    if observed_readme is None:
        os.rename(
            readme_backup,
            "README.md",
            src_dir_fd=repository_fd,
            dst_dir_fd=repository_fd,
        )
        _verify_regular_contents_at(
            repository_fd,
            "README.md",
            expected_content=expected_readme_before,
        )
        return
    _verify_regular_contents_at(
        repository_fd,
        "README.md",
        expected_content=expected_readme_before,
    )
    _cleanup_owned_if_present(repository_fd, readme_backup, readme_backup_identity)


def _rollback_catalog(
    repository_fd: int,
    *,
    stage_name: str,
    stage_identity: NodeIdentity,
    catalog_backup: str,
    catalog_backup_identity: NodeIdentity | None,
    catalog_installed: bool,
) -> None:
    if catalog_installed:
        _require_node_identity_at(repository_fd, "catalog", stage_identity)
        os.rename(
            "catalog",
            stage_name,
            src_dir_fd=repository_fd,
            dst_dir_fd=repository_fd,
        )
    if catalog_backup_identity is None:
        return
    if _stat_at(repository_fd, "catalog") is not None:
        raise PublicationError("catalog rollback could not restore the recovery backup")
    _require_node_identity_at(repository_fd, catalog_backup, catalog_backup_identity)
    os.rename(
        catalog_backup,
        "catalog",
        src_dir_fd=repository_fd,
        dst_dir_fd=repository_fd,
    )
    _require_node_identity_at(repository_fd, "catalog", catalog_backup_identity)


def _write_catalog_stage(root_fd: int, stage_name: str, contents: dict[str, bytes]) -> None:
    stage_fd = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        directories = sorted(
            {
                PurePosixPath(name).parent.as_posix()
                for name in contents
                if PurePosixPath(name).parent.as_posix() != "."
            }
        )
        for directory in directories:
            if "/" in directory:
                raise PublicationError("catalog artifact nesting is too deep")
            os.mkdir(directory, mode=0o755, dir_fd=stage_fd)
        for name, content in contents.items():
            _write_relative_regular(stage_fd, name, content)
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)


def _verify_catalog_stage(
    root_fd: int,
    stage_name: str,
    *,
    expected_identity: NodeIdentity,
    expected_contents: Mapping[str, bytes],
) -> None:
    _require_node_identity_at(root_fd, stage_name, expected_identity)
    stage_fd = os.open(
        stage_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    try:
        if _node_identity(os.fstat(stage_fd)) != expected_identity:
            raise PublicationError("catalog stage changed while it was opened")
        observed_files, observed_directories = _tree_entries(stage_fd)
        expected_files = set(expected_contents)
        expected_directories = {
            PurePosixPath(name).parent.as_posix()
            for name in expected_contents
            if PurePosixPath(name).parent.as_posix() != "."
        }
        if observed_files != expected_files or observed_directories != expected_directories:
            raise PublicationError("catalog stage file set changed before promotion")
        for name, expected_content in expected_contents.items():
            if _read_relative_regular(stage_fd, name) != expected_content:
                raise PublicationError("catalog stage bytes changed before promotion")
    finally:
        os.close(stage_fd)


def _write_relative_regular(root_fd: int, name: str, content: bytes) -> None:
    path = PurePosixPath(name)
    parent_fd = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        _write_regular_at(parent_fd, path.name, content)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _write_regular_at(parent_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o644, dir_fd=parent_fd)
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_entries(root_fd: int) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    entry_count = 0

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        nonlocal entry_count
        for name in os.listdir(directory_fd):
            entry_count += 1
            if entry_count > MAX_ARTIFACTS + 2:
                raise PublicationError("catalog source contains too many filesystem entries")
            details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = (prefix / name).as_posix()
            if stat.S_ISREG(details.st_mode):
                files.add(relative)
            elif stat.S_ISDIR(details.st_mode):
                if prefix.parts:
                    raise PublicationError("catalog source directory nesting is too deep")
                directories.add(relative)
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    visit(child_fd, prefix / name)
                finally:
                    os.close(child_fd)
            else:
                raise PublicationError("catalog source contains a non-regular entry")

    visit(root_fd, PurePosixPath())
    return files, directories


def _read_relative_regular(root_fd: int, name: str) -> bytes:
    path = PurePosixPath(name)
    parent_fd = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
        content, _ = _read_regular_at(parent_fd, path.name, max_bytes=MAX_FILE_BYTES)
        return content
    except OSError:
        raise PublicationError("catalog artifact could not be read safely") from None
    finally:
        os.close(parent_fd)


def _read_regular_at(parent_fd: int, name: str, *, max_bytes: int) -> tuple[bytes, FileIdentity]:
    before = _stat_at(parent_fd, name)
    if before is None or not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise PublicationError("required regular file is missing or exceeds its byte limit")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before) or not stat.S_ISREG(opened.st_mode):
            raise PublicationError("regular file changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PublicationError("regular file exceeds its byte limit")
            chunks.append(chunk)
        if _file_identity(os.fstat(descriptor)) != _file_identity(before):
            raise PublicationError("regular file changed while it was read")
    finally:
        os.close(descriptor)
    after = _stat_at(parent_fd, name)
    if after is None or _file_identity(after) != _file_identity(before):
        raise PublicationError("regular file changed while it was read")
    return b"".join(chunks), _file_identity(before)


def _open_directory(path: Path, label: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PublicationError(f"{label} must be a safe directory") from None


def _optional_directory_identity(parent_fd: int, name: str) -> NodeIdentity | None:
    details = _stat_at(parent_fd, name)
    if details is None:
        return None
    if not stat.S_ISDIR(details.st_mode):
        raise PublicationError("tracked catalog path must be a directory")
    return _node_identity(details)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _node_identity(details: os.stat_result) -> NodeIdentity:
    return (details.st_dev, details.st_ino, details.st_mode)


def _file_identity(details: os.stat_result) -> FileIdentity:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _required_node_identity_at(parent_fd: int, name: str) -> NodeIdentity:
    observed = _stat_at(parent_fd, name)
    if observed is None:
        raise PublicationError("owned publication path disappeared")
    return _node_identity(observed)


def _require_node_identity_at(parent_fd: int, name: str, expected: NodeIdentity) -> None:
    observed = _stat_at(parent_fd, name)
    if observed is None or _node_identity(observed) != expected:
        raise PublicationError("repository target changed during publication")


def _require_file_identity_at(parent_fd: int, name: str, expected: FileIdentity) -> None:
    observed = _stat_at(parent_fd, name)
    if observed is None or _file_identity(observed) != expected:
        raise PublicationError("repository file changed during publication")


def _verify_regular_contents_at(
    parent_fd: int,
    name: str,
    *,
    expected_content: bytes,
    expected_node_identity: NodeIdentity | None = None,
    expected_file_identity: FileIdentity | None = None,
) -> None:
    observed_content, observed_file_identity = _read_regular_at(
        parent_fd,
        name,
        max_bytes=max(MAX_README_BYTES, len(expected_content)),
    )
    if observed_content != expected_content:
        raise PublicationError("publication file bytes changed before promotion")
    if expected_node_identity is not None:
        observed_node_identity = observed_file_identity[:3]
        if observed_node_identity != expected_node_identity:
            raise PublicationError("publication file identity changed before promotion")
    if expected_file_identity is not None and observed_file_identity != expected_file_identity:
        raise PublicationError("repository file changed during publication")


def _require_missing(parent_fd: int, name: str) -> None:
    if _stat_at(parent_fd, name) is not None:
        raise PublicationError("exclusive publication path already exists")


def _cleanup_owned_if_present(
    parent_fd: int,
    name: str,
    expected_identity: NodeIdentity | FileIdentity,
) -> None:
    observed = _stat_at(parent_fd, name)
    if observed is None:
        return
    observed_identity: NodeIdentity | FileIdentity
    if len(expected_identity) == 3:
        observed_identity = _node_identity(observed)
    else:
        observed_identity = _file_identity(observed)
    if observed_identity != expected_identity:
        raise PublicationError("owned cleanup path was replaced and was left intact")
    _remove_entry_at(parent_fd, name)


def _remove_entry_at(parent_fd: int, name: str) -> None:
    details = _stat_at(parent_fd, name)
    if details is None:
        return
    if stat.S_ISDIR(details.st_mode):
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            for child in os.listdir(directory_fd):
                _remove_entry_at(directory_fd, child)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)
