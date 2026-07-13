"""Deterministic catalog renderers and complete-directory publication."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from github_module_catalog.models import CapabilityDefinition, CatalogEntry, CatalogManifest
from github_module_catalog.safeio import (
    FileIdentity,
    file_identity,
    make_directory_at,
    open_directory,
    remove_tree_at,
    require_identity,
    stat_entry,
    write_regular_file_at,
)
from github_module_catalog.safeio import (
    UnsafeOutputPathError as UnsafeOutputPathError,
)


class CatalogFormat(StrEnum):
    """One independently selectable catalog representation."""

    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "markdown"


ALL_CATALOG_FORMATS = frozenset(CatalogFormat)


def render_catalog_json(manifest: CatalogManifest) -> bytes:
    """Render canonical UTF-8 JSON with one trailing newline."""

    return _canonical_json(_catalog_document(manifest))


def render_catalog_yaml(manifest: CatalogManifest) -> bytes:
    """Render canonical pretty JSON, a YAML 1.2-compatible catalog representation."""

    return _canonical_pretty_json(_catalog_document(manifest))


def render_readme(manifest: CatalogManifest) -> str:
    """Render the deterministic human catalog without untrusted descriptions."""

    lines = [
        "# GitHub Module Catalog",
        "",
        _untrusted_markdown_inline(manifest.coverage_note),
        "",
        "[Capability taxonomy](taxonomy.md)",
        "",
    ]
    if manifest.selection is not None:
        lines.extend(_selection_summary(manifest))
        lines.extend(
            [
                f"Source: {_markdown_code_span(manifest.source)}.",
                "",
                "| Rank | Stars | Last push | Repository | Capabilities | License | Reuse status |",
                "| ---: | ---: | --- | --- | --- | --- | --- |",
            ]
        )
        lines.extend(_ranked_entry_row(entry) for entry in manifest.entries)
    else:
        lines.extend(
            [
                f"Source: {_markdown_code_span(manifest.source)}; cursor: "
                f"`{manifest.cursor_start}` through `{manifest.cursor_end}`.",
                "",
                "| Repository ID | Repository | Capabilities | License | Reuse status |",
                "| ---: | --- | --- | --- | --- |",
            ]
        )
        lines.extend(_legacy_entry_row(entry) for entry in manifest.entries)
    return _newline("\n".join(lines))


def render_taxonomy_page(manifest: CatalogManifest) -> str:
    """Render a deterministic nested capability map with repository counts."""

    definitions = {definition.id: definition for definition in manifest.capability_definitions}
    children = {
        capability_id: tuple(
            sorted(
                definition.id
                for definition in manifest.capability_definitions
                if capability_id in definition.parents
            )
        )
        for capability_id in definitions
    }
    counts = _capability_counts(manifest)
    roots = tuple(
        definition.id for definition in manifest.capability_definitions if not definition.parents
    )
    lines = [
        "# Capability taxonomy",
        "",
        f"Taxonomy version: {_markdown_code_span(manifest.taxonomy_version)}. "
        f"Classifier: {_markdown_code_span(manifest.classifier_version)}.",
        "",
        "Repositories can appear in multiple capability branches. Parent counts include "
        "repositories assigned to descendant capabilities.",
        "",
        "## Capability map",
        "",
    ]
    for capability_id in roots:
        lines.extend(
            _taxonomy_branch_lines(
                capability_id,
                depth=0,
                definitions=definitions,
                children=children,
                counts=counts,
            )
        )
    if not roots:
        lines.append("No capability definitions were published for this snapshot.")
    return _newline("\n".join(lines))


def render_module_page(manifest: CatalogManifest, capability_id: str) -> str:
    """Render one capability-specific Markdown page."""

    matching = tuple(
        (entry, assertion)
        for entry in manifest.entries
        for assertion in entry.assertions
        if assertion.capability_id == capability_id
    )
    lines = [f"# {_markdown_code_span(capability_id)} modules", ""]
    if manifest.selection is not None:
        lines.extend(_selection_summary(manifest))
    if manifest.selection is None:
        lines.extend(
            [
                "| Repository ID | Repository | Confidence | License | Reuse status |",
                "| ---: | --- | ---: | --- | --- |",
            ]
        )
    else:
        lines.extend(
            [
                "| Rank | Stars | Last push | Repository | Confidence | License | Reuse status |",
                "| ---: | ---: | --- | --- | ---: | --- | --- |",
            ]
        )
    for entry, assertion in matching:
        if manifest.selection is None:
            lines.append(
                "| "
                f"{entry.repository.identity.repository_id} | {_repository_link(entry)} | "
                f"{assertion.confidence:.2f} | {_license(entry)} | "
                f"`{assertion.reuse_status.value}` |"
            )
        else:
            lines.append(
                "| "
                f"{_rank(entry)} | {_stars(entry)} | {_last_push(entry)} | "
                f"{_repository_link(entry)} | "
                f"{assertion.confidence:.2f} | {_license(entry)} | "
                f"`{assertion.reuse_status.value}` |"
            )
    return _newline("\n".join(lines))


def render_publication_manifest(
    manifest: CatalogManifest,
    *,
    formats: frozenset[CatalogFormat] = ALL_CATALOG_FORMATS,
) -> bytes:
    """Render the exact artifact manifest bytes for a selected format set."""

    artifacts = _publication_artifacts(manifest, _validate_formats(formats))
    return artifacts[Path("manifest.json")]


def publish_catalog(
    manifest: CatalogManifest,
    output_dir: Path,
    *,
    formats: frozenset[CatalogFormat] = ALL_CATALOG_FORMATS,
    trusted_parent_fd: int | None = None,
) -> tuple[Path, ...]:
    """Publish a complete catalog directory, never an in-progress build."""

    selected = _validate_formats(formats)
    output = Path(output_dir).expanduser().absolute()
    if output.name in {"", ".", ".."}:
        raise UnsafeOutputPathError("output directory must have a simple name")
    parent = output.parent
    if trusted_parent_fd is None:
        parent.mkdir(parents=True, exist_ok=True)
        parent_fd = open_directory(parent)
    else:
        parent_fd = os.dup(trusted_parent_fd)
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            os.close(parent_fd)
            raise UnsafeOutputPathError("trusted publication parent is not a directory")
    output_details = stat_entry(parent_fd, output.name)
    if output_details is not None and not stat.S_ISDIR(output_details.st_mode):
        os.close(parent_fd)
        raise UnsafeOutputPathError("output path must be a directory")
    output_identity = None if output_details is None else file_identity(output_details)
    stage_name = f".{output.name}.stage-{uuid.uuid4().hex}"
    stage_fd, stage_identity = make_directory_at(parent_fd, stage_name)
    try:
        artifacts = _publication_artifacts(manifest, selected)
        for relative_path, content in artifacts.items():
            write_regular_file_at(stage_fd, relative_path, content)
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = -1
        _publish_directory_at(
            parent_fd,
            stage_name=stage_name,
            stage_identity=stage_identity,
            output_name=output.name,
            output_identity=output_identity,
        )
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)
        remaining_stage = stat_entry(parent_fd, stage_name)
        if remaining_stage is not None and file_identity(remaining_stage) == stage_identity:
            remove_tree_at(parent_fd, stage_name, expected=stage_identity)
        os.close(parent_fd)
    return tuple(output / relative_path for relative_path in artifacts)


def _validate_formats(formats: frozenset[CatalogFormat]) -> frozenset[CatalogFormat]:
    if not isinstance(formats, frozenset):
        raise TypeError("formats must be an immutable frozenset")
    if not formats or any(not isinstance(item, CatalogFormat) for item in formats):
        raise ValueError("formats must contain at least one supported catalog format")
    return formats


def _publication_artifacts(
    manifest: CatalogManifest, formats: frozenset[CatalogFormat]
) -> dict[Path, bytes]:
    capabilities = sorted(
        {assertion.capability_id for entry in manifest.entries for assertion in entry.assertions}
    )
    artifacts: dict[Path, bytes] = {}
    if CatalogFormat.JSON in formats:
        artifacts[Path("catalog.json")] = render_catalog_json(manifest)
    if CatalogFormat.YAML in formats:
        artifacts[Path("catalog.yaml")] = render_catalog_yaml(manifest)
    if CatalogFormat.MARKDOWN in formats:
        artifacts[Path("README.md")] = render_readme(manifest).encode("utf-8")
        artifacts[Path("taxonomy.md")] = render_taxonomy_page(manifest).encode("utf-8")
        artifacts.update(
            {
                Path("modules") / f"{capability}.md": render_module_page(
                    manifest, capability
                ).encode("utf-8")
                for capability in capabilities
            }
        )
    artifacts[Path("manifest.json")] = _canonical_json(_manifest_document(manifest, artifacts))
    return dict(sorted(artifacts.items(), key=lambda item: item[0].as_posix()))


def _capability_counts(manifest: CatalogManifest) -> dict[str, int]:
    capability_ids = sorted(
        {assertion.capability_id for entry in manifest.entries for assertion in entry.assertions}
    )
    return {
        capability_id: sum(
            assertion.capability_id == capability_id
            for entry in manifest.entries
            for assertion in entry.assertions
        )
        for capability_id in capability_ids
    }


def _taxonomy_branch_lines(
    capability_id: str,
    *,
    depth: int,
    definitions: dict[str, CapabilityDefinition],
    children: dict[str, tuple[str, ...]],
    counts: dict[str, int],
) -> list[str]:
    definition = definitions[capability_id]
    label = _markdown_text(definition.label)
    count = counts.get(capability_id, 0)
    reference = _markdown_code_span(capability_id)
    if count:
        reference = f"[{reference}](modules/{capability_id}.md)"
    lines = [f"{'  ' * depth}- {reference} — {label} — {count:,}"]
    for child_id in children[capability_id]:
        lines.extend(
            _taxonomy_branch_lines(
                child_id,
                depth=depth + 1,
                definitions=definitions,
                children=children,
                counts=counts,
            )
        )
    return lines


def _catalog_document(manifest: CatalogManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json", exclude_none=True)


def _manifest_document(
    manifest: CatalogManifest, artifacts: dict[Path, bytes]
) -> dict[str, object]:
    document = manifest.model_dump(mode="json", exclude={"entries"}, exclude_none=True)
    document["artifacts"] = {
        path.as_posix(): hashlib.sha256(content).hexdigest()
        for path, content in sorted(artifacts.items(), key=lambda item: item[0].as_posix())
    }
    return document


def _ranked_entry_row(entry: CatalogEntry) -> str:
    capabilities = ", ".join(
        f"`{_markdown_text(assertion.capability_id)}`" for assertion in entry.assertions
    )
    return (
        "| "
        f"{_rank(entry)} | {_stars(entry)} | {_last_push(entry)} | "
        f"{_repository_link(entry)} | "
        f"{capabilities or '—'} | {_license(entry)} | "
        f"`{entry.repository.reuse_status.value}` |"
    )


def _legacy_entry_row(entry: CatalogEntry) -> str:
    capabilities = ", ".join(
        f"`{_markdown_text(assertion.capability_id)}`" for assertion in entry.assertions
    )
    return (
        "| "
        f"{entry.repository.identity.repository_id} | {_repository_link(entry)} | "
        f"{capabilities or '—'} | {_license(entry)} | "
        f"`{entry.repository.reuse_status.value}` |"
    )


def _selection_summary(manifest: CatalogManifest) -> list[str]:
    selection = manifest.selection
    if selection is None:
        return []
    return [
        "## Selection",
        "",
        f"Minimum stars: `{selection.min_stars}`; "
        f"Pushed since: `{_utc_timestamp(selection.pushed_since)}`.",
        "Archived: `false`; forks: `false`; visibility: `public`; "
        f"Order: `{selection.sort} {selection.order}`.",
        f"Top `{manifest.entry_count}` of `{manifest.api_total_count}` matching repositories; "
        f"result limit: `{manifest.result_limit}`; pages fetched: `{manifest.pages_fetched}`.",
        "",
    ]


def _rank(entry: CatalogEntry) -> str:
    return "—" if entry.rank is None else str(entry.rank)


def _stars(entry: CatalogEntry) -> str:
    stars = entry.repository.stargazers_count
    return "—" if stars is None else str(stars)


def _last_push(entry: CatalogEntry) -> str:
    pushed_at = entry.repository.pushed_at
    return "—" if pushed_at is None else _utc_timestamp(pushed_at)


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _repository_link(entry: CatalogEntry) -> str:
    label = _markdown_text(entry.repository.full_name)
    return f"[{label}]({entry.repository.html_url})"


def _license(entry: CatalogEntry) -> str:
    return _markdown_code_span(entry.repository.license_spdx or "unknown")


def _markdown_text(value: str) -> str:
    value = html.escape(value, quote=True)
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _markdown_code_span(value: str) -> str:
    collapsed = html.escape(re.sub(r"[\r\n]+", " ", value), quote=True)
    longest_run = max((len(run) for run in re.findall(r"`+", collapsed)), default=0)
    delimiter = "`" * (longest_run + 1)
    if longest_run == 0 and not collapsed.startswith(" ") and not collapsed.endswith(" "):
        return f"{delimiter}{collapsed}{delimiter}"
    return f"{delimiter} {collapsed} {delimiter}"


def _untrusted_markdown_inline(value: str) -> str:
    collapsed = re.sub(r"[\r\n]+", " ", value)
    html_safe = html.escape(collapsed, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", html_safe)


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_pretty_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _newline(value: str) -> str:
    return value.rstrip("\n") + "\n"


def _publish_directory_at(
    parent_fd: int,
    *,
    stage_name: str,
    stage_identity: FileIdentity,
    output_name: str,
    output_identity: FileIdentity | None,
) -> None:
    backup_name: str | None = None
    backup_identity: FileIdentity | None = None
    if output_identity is not None:
        require_identity(
            stat_entry(parent_fd, output_name),
            output_identity,
            message="output changed during publication",
        )
        backup_name = f".{output_name}.backup-{uuid.uuid4().hex}"
        os.rename(
            output_name,
            backup_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        require_identity(
            stat_entry(parent_fd, backup_name),
            output_identity,
            message="output changed during publication",
        )
        backup_identity = output_identity
    require_identity(
        stat_entry(parent_fd, stage_name),
        stage_identity,
        message="staging directory changed during publication",
    )
    try:
        os.rename(
            stage_name,
            output_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        require_identity(
            stat_entry(parent_fd, output_name),
            stage_identity,
            message="staging directory changed during publication",
        )
    except Exception:
        if (
            backup_name is not None
            and backup_identity is not None
            and stat_entry(parent_fd, output_name) is None
        ):
            require_identity(
                stat_entry(parent_fd, backup_name),
                backup_identity,
                message="backup changed during publication rollback",
            )
            os.rename(
                backup_name,
                output_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        raise
    os.fsync(parent_fd)
    if backup_name is not None and backup_identity is not None:
        remove_tree_at(parent_fd, backup_name, expected=backup_identity)
        os.fsync(parent_fd)
