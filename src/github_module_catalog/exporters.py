"""Deterministic catalog renderers and complete-directory publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from github_module_catalog.models import CatalogEntry, CatalogManifest


class UnsafeOutputPathError(ValueError):
    """Raised when publication could follow an unsafe output path."""


def render_catalog_json(manifest: CatalogManifest) -> bytes:
    """Render canonical UTF-8 JSON with one trailing newline."""

    return _canonical_json(_catalog_document(manifest))


def render_catalog_yaml(manifest: CatalogManifest) -> bytes:
    """Render stable UTF-8 YAML equivalent to the JSON catalog."""

    rendered = yaml.safe_dump(
        _catalog_document(manifest),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    return _newline(rendered).encode("utf-8")


def render_readme(manifest: CatalogManifest) -> str:
    """Render the deterministic human catalog without untrusted descriptions."""

    lines = [
        "# GitHub Module Catalog",
        "",
        manifest.coverage_note,
        "",
        f"Source: `{_markdown_text(manifest.source)}`; cursor: "
        f"`{manifest.cursor_start}` through `{manifest.cursor_end}`.",
        "",
        "| Repository ID | Repository | Capabilities | License | Reuse status |",
        "| ---: | --- | --- | --- | --- |",
    ]
    lines.extend(_entry_row(entry) for entry in manifest.entries)
    return _newline("\n".join(lines))


def render_module_page(manifest: CatalogManifest, capability_id: str) -> str:
    """Render one capability-specific Markdown page."""

    matching = tuple(
        (entry, assertion)
        for entry in manifest.entries
        for assertion in entry.assertions
        if assertion.capability_id == capability_id
    )
    lines = [
        f"# `{_markdown_text(capability_id)}` modules",
        "",
        "| Repository ID | Repository | Confidence | License | Reuse status |",
        "| ---: | --- | ---: | --- | --- |",
    ]
    for entry, assertion in matching:
        lines.append(
            "| "
            f"{entry.repository.identity.repository_id} | {_repository_link(entry)} | "
            f"{assertion.confidence:.2f} | {_license(entry)} | "
            f"`{assertion.reuse_status.value}` |"
        )
    return _newline("\n".join(lines))


def publish_catalog(manifest: CatalogManifest, output_dir: Path) -> tuple[Path, ...]:
    """Publish a complete catalog directory, never an in-progress build."""

    output = Path(output_dir)
    if output.is_symlink():
        raise UnsafeOutputPathError("output directory must not be a symbolic link")
    parent = output.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not output.is_dir():
        raise UnsafeOutputPathError("output path must be a directory")
    stage = parent / f".{output.name}.stage-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        artifacts = _publication_artifacts(manifest)
        for relative_path, content in artifacts.items():
            _atomic_write(_safe_target(stage, relative_path), content)
        _publish_directory(stage, output.resolve(strict=False))
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return tuple(output / relative_path for relative_path in artifacts)


def _publication_artifacts(manifest: CatalogManifest) -> dict[Path, bytes]:
    capabilities = sorted(
        {assertion.capability_id for entry in manifest.entries for assertion in entry.assertions}
    )
    artifacts = {
        Path("catalog.json"): render_catalog_json(manifest),
        Path("catalog.yaml"): render_catalog_yaml(manifest),
        Path("README.md"): render_readme(manifest).encode("utf-8"),
    }
    artifacts.update(
        {
            Path("modules") / f"{capability}.md": render_module_page(manifest, capability).encode(
                "utf-8"
            )
            for capability in capabilities
        }
    )
    artifacts[Path("manifest.json")] = _canonical_json(_manifest_document(manifest, artifacts))
    return dict(sorted(artifacts.items(), key=lambda item: item[0].as_posix()))


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


def _entry_row(entry: CatalogEntry) -> str:
    capabilities = ", ".join(
        f"`{_markdown_text(assertion.capability_id)}`" for assertion in entry.assertions
    )
    return (
        "| "
        f"{entry.repository.identity.repository_id} | {_repository_link(entry)} | "
        f"{capabilities or '—'} | {_license(entry)} | "
        f"`{entry.repository.reuse_status.value}` |"
    )


def _repository_link(entry: CatalogEntry) -> str:
    label = _markdown_text(entry.repository.full_name)
    return f"[{label}]({entry.repository.html_url})"


def _license(entry: CatalogEntry) -> str:
    return f"`{_markdown_text(entry.repository.license_spdx or 'unknown')}`"


def _markdown_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _newline(value: str) -> str:
    return value.rstrip("\n") + "\n"


def _safe_target(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise UnsafeOutputPathError("artifact path must remain inside the publication root")
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.resolve(strict=False).is_relative_to(root.resolve()):
        raise UnsafeOutputPathError("artifact path resolves outside the publication root")
    return target


def _atomic_write(target: Path, content: bytes) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, target)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _publish_directory(stage: Path, output: Path) -> None:
    if not output.exists():
        os.replace(stage, output)
        return
    backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"
    os.replace(output, backup)
    try:
        os.replace(stage, output)
    except Exception:
        os.replace(backup, output)
        raise
    shutil.rmtree(backup)
