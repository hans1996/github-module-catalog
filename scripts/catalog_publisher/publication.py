"""Repository publication orchestration with injectable promotion boundaries."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from types import TracebackType

from .constants import MAX_README_BYTES
from .filesystem import (
    _cleanup_owned_if_present,
    _open_directory,
    _optional_directory_identity,
    _promote_transaction,
    _read_regular_at,
    _require_missing,
    _required_node_identity_at,
    _write_catalog_stage,
    _write_regular_at,
)
from .model import FileIdentity, NodeIdentity, PublicationError, PublicationSummary
from .validation import _replace_managed_section, _validate_source


def publish_to_repository(source: Path, repository: Path) -> PublicationSummary:
    """Validate source bytes, then transactionally update catalog/ and README.md."""

    source_path = Path(source).expanduser().absolute()
    repository_path = Path(repository).expanduser().absolute()
    validated = _validate_source(source_path)
    repository_fd = _open_directory(repository_path, "repository")
    stage_name = f".catalog-stage-{secrets.token_hex(16)}"
    readme_temp = f".README.md.stage-{secrets.token_hex(16)}"
    catalog_backup = f".catalog-backup-{secrets.token_hex(16)}"
    readme_backup = f".README.md.backup-{secrets.token_hex(16)}"
    stage_identity: NodeIdentity | None = None
    readme_temp_identity: FileIdentity | None = None
    failure: BaseException | None = None
    failure_traceback: TracebackType | None = None
    try:
        readme_before, readme_identity = _read_regular_at(
            repository_fd,
            "README.md",
            max_bytes=MAX_README_BYTES,
        )
        updated_readme = _replace_managed_section(readme_before, validated.homepage)
        catalog_identity = _optional_directory_identity(repository_fd, "catalog")
        _require_missing(repository_fd, stage_name)
        _require_missing(repository_fd, readme_temp)
        _require_missing(repository_fd, catalog_backup)
        _require_missing(repository_fd, readme_backup)
        os.mkdir(stage_name, mode=0o700, dir_fd=repository_fd)
        stage_identity = _required_node_identity_at(repository_fd, stage_name)
        _write_catalog_stage(repository_fd, stage_name, validated.contents)
        _write_regular_at(repository_fd, readme_temp, updated_readme)
        _, readme_temp_identity = _read_regular_at(
            repository_fd,
            readme_temp,
            max_bytes=MAX_README_BYTES,
        )
        _promote_transaction(
            repository_fd,
            stage_name=stage_name,
            readme_temp=readme_temp,
            catalog_backup=catalog_backup,
            readme_backup=readme_backup,
            stage_identity=stage_identity,
            readme_temp_identity=readme_temp_identity,
            catalog_identity=catalog_identity,
            readme_identity=readme_identity,
            expected_catalog=validated.contents,
            expected_readme_before=readme_before,
            expected_readme_after=updated_readme,
        )
    except BaseException as error:
        failure = error
        failure_traceback = error.__traceback__

    cleanup_errors: list[BaseException] = []
    for name, identity in (
        (stage_name, stage_identity),
        (readme_temp, readme_temp_identity),
    ):
        if identity is None:
            continue
        try:
            _cleanup_owned_if_present(repository_fd, name, identity)
        except BaseException as error:
            cleanup_errors.append(error)
    try:
        os.close(repository_fd)
    except BaseException as error:
        cleanup_errors.append(error)

    if failure is not None:
        for cleanup_error in cleanup_errors:
            failure.add_note(f"cleanup also failed: {cleanup_error}")
        raise failure.with_traceback(failure_traceback)
    if cleanup_errors:
        raise PublicationError("catalog publication cleanup failed safely") from cleanup_errors[0]
    return PublicationSummary(
        entries=validated.entries,
        capabilities=validated.capabilities,
        catalog_files=len(validated.contents),
    )
