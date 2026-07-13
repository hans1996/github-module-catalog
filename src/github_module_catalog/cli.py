"""Safe command-line operations for a bounded local catalog workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol, cast, runtime_checkable

import typer
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from github_module_catalog.catalog import (
    CatalogBuildContext,
    Classifier,
    build_catalog,
    build_catalog_from_state,
)
from github_module_catalog.exporters import CatalogFormat, UnsafeOutputPathError, publish_catalog
from github_module_catalog.github import GitHubRepositorySource
from github_module_catalog.models import CatalogManifest
from github_module_catalog.scanner import DiscoveryScanner, ScanStatus
from github_module_catalog.source import RepositorySource
from github_module_catalog.state import CatalogStateSnapshot, StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import classify_repository, load_taxonomy

_SOURCE_NAME = "github"
_MAX_PAGES = 1_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

WorkspaceOption = Annotated[Path, typer.Option("--workspace", help="Local catalog workspace.")]


class CliOperationError(RuntimeError):
    """A safe, user-facing operational failure."""


@runtime_checkable
class _Closable(Protocol):
    def close(self) -> None: ...


def _default_source_factory(token: str) -> RepositorySource:
    return GitHubRepositorySource(token=token)


def _default_taxonomy_path() -> Path:
    return Path(__file__).parents[2] / "config" / "taxonomy.yaml"


@dataclass(frozen=True, slots=True)
class CliDependencies:
    """Injectable boundaries used by command-level tests and production."""

    source_factory: Callable[[str], RepositorySource] = _default_source_factory
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    classifier: Classifier = classify_repository
    taxonomy_path: Path = field(default_factory=_default_taxonomy_path)


def create_app(dependencies: CliDependencies | None = None) -> typer.Typer:
    """Create an isolated Typer app with explicit runtime dependencies."""

    deps = dependencies or CliDependencies()
    cli = typer.Typer(help="Build a traceable catalog of public GitHub repositories.")

    @cli.command("init")
    def initialize(workspace: WorkspaceOption) -> None:
        _command(lambda: _initialize(workspace))

    @cli.command()
    def discover(
        workspace: WorkspaceOption,
        max_pages: Annotated[
            int,
            typer.Option("--max-pages", min=1, max=_MAX_PAGES, help="Required page budget."),
        ],
    ) -> None:
        _command(lambda: _discover(workspace, max_pages=max_pages, dependencies=deps))

    @cli.command()
    def status(workspace: WorkspaceOption) -> None:
        _command(lambda: _status(workspace))

    @cli.command()
    def classify(workspace: WorkspaceOption) -> None:
        _command(lambda: _classify(workspace, dependencies=deps))

    @cli.command()
    def build(
        workspace: WorkspaceOption,
        formats: Annotated[list[str] | None, typer.Option("--format")] = None,
    ) -> None:
        _command(lambda: _build(workspace, formats=formats, dependencies=deps))

    @cli.command()
    def validate(workspace: WorkspaceOption) -> None:
        _command(lambda: _validate(workspace, dependencies=deps))

    return cli


def _command(operation: Callable[[], Mapping[str, object]]) -> None:
    try:
        result = operation()
    except CliOperationError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, sqlite3.Error, ValidationError, UnsafeOutputPathError, yaml.YAMLError):
        typer.echo("Error: operation failed safely", err=True)
        raise typer.Exit(code=1) from None
    except Exception as error:
        typer.echo(f"Error: operation failed ({type(error).__name__})", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(dict(result), sort_keys=True, separators=(",", ":")))


def _safe_workspace(path: Path, *, create: bool = False) -> Path:
    candidate = path.expanduser().absolute()
    if candidate.is_symlink():
        raise CliOperationError("workspace must not be a symbolic link")
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise CliOperationError("filesystem root is not a workspace")
    if resolved.exists() and not resolved.is_dir():
        raise CliOperationError("workspace must be a directory")
    if create:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not resolved.is_dir():
        raise CliOperationError("workspace is not initialized")
    return resolved


def _state_path(workspace: Path) -> Path:
    return workspace / "data" / "state.sqlite3"


def _safe_state_path(workspace: Path) -> Path:
    data_directory = workspace / "data"
    state_path = _state_path(workspace)
    if data_directory.is_symlink() or state_path.is_symlink():
        raise CliOperationError("workspace state storage must not be a symbolic link")
    if data_directory.exists() and not data_directory.is_dir():
        raise CliOperationError("workspace state storage must be a directory")
    return state_path


@contextmanager
def _stores(
    path: Path, *, create: bool = False
) -> Iterator[tuple[Path, RawObjectStore, StateStore]]:
    workspace = _safe_workspace(path, create=create)
    state_path = _safe_state_path(workspace)
    if not create and not state_path.is_file():
        raise CliOperationError("workspace is not initialized")
    raw_store = RawObjectStore(workspace)
    state = StateStore(state_path, raw_store)
    try:
        yield workspace, raw_store, state
    finally:
        state.close()


def _initialize(path: Path) -> Mapping[str, object]:
    with _stores(path, create=True) as (workspace, _raw_store, _state):
        return {"status": "initialized", "workspace": str(workspace)}


def _github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token is None or not token.strip():
        raise CliOperationError("GITHUB_TOKEN is required for discovery")
    return token


def _discover(path: Path, *, max_pages: int, dependencies: CliDependencies) -> Mapping[str, object]:
    token = _github_token()
    with _stores(path) as (_workspace, raw_store, state):
        source = dependencies.source_factory(token)
        try:
            outcome = DiscoveryScanner(
                source=source,
                raw_store=raw_store,
                state=state,
                source_name=_SOURCE_NAME,
            ).scan(max_pages=max_pages, started_at=dependencies.now())
        finally:
            if isinstance(source, _Closable):
                source.close()
    if outcome.status is ScanStatus.ERROR:
        raise CliOperationError(f"discovery failed ({outcome.error_type or 'unknown error'})")
    return {
        "status": outcome.status.value,
        "cursor_end": outcome.cursor_end,
        "pages_committed": outcome.pages_committed,
        "observations_recorded": outcome.observations_recorded,
        "observation_failures": outcome.observation_failures,
    }


def _snapshot_summary(snapshot: CatalogStateSnapshot) -> Mapping[str, object]:
    return {
        "cursor_end": snapshot.cursor_end,
        "discovered": snapshot.discovered_count,
        "observations": snapshot.validated_observation_count,
        "pending": snapshot.pending_count,
        "retry": snapshot.retry_count,
        "dead_letter": snapshot.dead_letter_count,
    }


def _status(path: Path) -> Mapping[str, object]:
    with _stores(path) as (_workspace, _raw_store, state):
        return _snapshot_summary(state.catalog_snapshot(_SOURCE_NAME))


def _manifest(state: StateStore, dependencies: CliDependencies) -> CatalogManifest:
    taxonomy = load_taxonomy(dependencies.taxonomy_path)
    return build_catalog_from_state(
        state,
        taxonomy=taxonomy,
        source=_SOURCE_NAME,
        classifier=dependencies.classifier,
    )


def _classify(path: Path, *, dependencies: CliDependencies) -> Mapping[str, object]:
    with _stores(path) as (_workspace, _raw_store, state):
        manifest = _manifest(state, dependencies)
    return {
        "entries": manifest.entry_count,
        "capabilities": manifest.capability_count,
        "classification_failures": len(manifest.classification_failure_repository_ids),
        "writes": False,
    }


def _validated_formats(formats: list[str] | None) -> frozenset[CatalogFormat]:
    requested = tuple(formats or sorted(item.value for item in CatalogFormat))
    if len(requested) != len(set(requested)):
        raise CliOperationError("formats must be unique: json, yaml, markdown")
    try:
        selected = frozenset(CatalogFormat(item) for item in requested)
    except ValueError:
        raise CliOperationError("formats must be unique: json, yaml, markdown") from None
    if not selected.intersection({CatalogFormat.JSON, CatalogFormat.YAML}):
        raise CliOperationError("build requires at least one of: json, yaml")
    return selected


def _build(
    path: Path, *, formats: list[str] | None, dependencies: CliDependencies
) -> Mapping[str, object]:
    selected = _validated_formats(formats)
    with _stores(path) as (workspace, _raw_store, state):
        manifest = _manifest(state, dependencies)
        output = workspace / "catalog-output"
        artifacts = publish_catalog(manifest, output, formats=selected)
    return {
        "artifacts": len(artifacts),
        "entries": manifest.entry_count,
        "formats": sorted(item.value for item in selected),
        "output": str(output.resolve()),
    }


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CliOperationError("catalog JSON could not be checked") from None
    if not isinstance(document, dict):
        raise CliOperationError("catalog JSON must contain an object")
    return cast(dict[str, object], document)


def _catalog_model_input(document: dict[str, object]) -> dict[str, object]:
    schema_input = dict(document)
    if "entry_count" not in schema_input or "capability_count" not in schema_input:
        raise CliOperationError("catalog computed counts are missing")
    schema_input.pop("entry_count")
    schema_input.pop("capability_count")
    entries = schema_input.get("entries")
    if not isinstance(entries, list):
        raise CliOperationError("catalog entries could not be checked")
    clean_entries: list[object] = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("repository"), dict):
            raise CliOperationError("catalog entry could not be checked")
        clean_item = dict(item)
        repository = dict(cast(dict[object, object], item["repository"]))
        if "reuse_status" not in repository:
            raise CliOperationError("catalog reuse status is missing")
        repository.pop("reuse_status")
        clean_item["repository"] = repository
        clean_entries.append(clean_item)
    schema_input["entries"] = clean_entries
    return schema_input


def _documents_match(left: object, right: object) -> bool:
    return json.dumps(
        left, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ) == json.dumps(right, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_catalog_document(document: dict[str, object]) -> CatalogManifest:
    try:
        catalog = CatalogManifest.model_validate(_catalog_model_input(document))
    except ValidationError:
        raise CliOperationError("catalog schema check failed") from None
    canonical = catalog.model_dump(mode="json", exclude_none=True)
    for key, expected in (
        ("entry_count", catalog.entry_count),
        ("capability_count", catalog.capability_count),
    ):
        observed = document.get(key)
        if type(observed) is not int or observed != expected:
            raise CliOperationError("catalog computed counts differ from validated entries")
    if not _documents_match(canonical, document):
        raise CliOperationError("catalog computed semantics differ from validated facts")
    return catalog


def _artifact_mapping(manifest: dict[str, object]) -> dict[str, str]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CliOperationError("artifact manifest could not be checked")
    mapping: dict[str, str] = {}
    for raw_name, raw_digest in artifacts.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise CliOperationError("artifact manifest could not be checked")
        mapping[raw_name] = raw_digest
    return mapping


def _expected_artifacts(catalog: CatalogManifest, artifacts: dict[str, str]) -> set[str]:
    expected = {name for name in ("catalog.json", "catalog.yaml") if name in artifacts}
    if not expected:
        raise CliOperationError("validation requires a JSON or YAML catalog")
    module_artifacts = {
        f"modules/{assertion.capability_id}.md"
        for entry in catalog.entries
        for assertion in entry.assertions
    }
    if "README.md" in artifacts:
        expected.update({"README.md", *module_artifacts})
    return expected


def _artifact_hashes(output: Path, artifacts: dict[str, str]) -> int:
    checked = 0
    for raw_name, raw_digest in artifacts.items():
        relative = Path(raw_name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or _SHA256.fullmatch(raw_digest) is None
        ):
            raise CliOperationError("artifact manifest contains an unsafe entry")
        target = output / relative
        if (
            target.is_symlink()
            or not target.is_file()
            or not target.resolve().is_relative_to(output)
        ):
            raise CliOperationError("required catalog artifact is unsafe or missing")
        observed = hashlib.sha256(target.read_bytes()).hexdigest()
        if observed != raw_digest:
            raise CliOperationError("catalog artifact hash differs")
        checked += 1
    actual_files: set[str] = set()
    for path in output.rglob("*"):
        if path.is_symlink():
            raise CliOperationError("catalog output contains a symbolic link")
        if path.is_file():
            actual_files.add(path.relative_to(output).as_posix())
    if actual_files != set(artifacts) | {"manifest.json"}:
        raise CliOperationError("catalog artifact list differs from output")
    return checked


def _read_yaml_object(path: Path) -> dict[str, object]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise CliOperationError("catalog YAML could not be checked") from None
    if not isinstance(document, dict):
        raise CliOperationError("catalog YAML must contain an object")
    return cast(dict[str, object], document)


def _machine_catalogs(output: Path, artifacts: dict[str, str]) -> tuple[dict[str, object], ...]:
    documents: list[dict[str, object]] = []
    if "catalog.json" in artifacts:
        documents.append(_read_json_object(output / "catalog.json"))
    if "catalog.yaml" in artifacts:
        documents.append(_read_yaml_object(output / "catalog.yaml"))
    if not documents:
        raise CliOperationError("validation requires a JSON or YAML catalog")
    if len(documents) == 2 and not _documents_match(documents[0], documents[1]):
        raise CliOperationError("catalog JSON and YAML differ")
    return tuple(documents)


def _validate_documents(output: Path) -> tuple[CatalogManifest, int]:
    manifest = _read_json_object(output / "manifest.json")
    artifacts = _artifact_mapping(manifest)
    checked = _artifact_hashes(output, artifacts)
    documents = _machine_catalogs(output, artifacts)
    validated_documents = tuple(_validate_catalog_document(document) for document in documents)
    validated = validated_documents[0]
    if any(item != validated for item in validated_documents[1:]):
        raise CliOperationError("catalog models differ")
    catalog_document = documents[0]
    catalog_manifest_fields = {
        key: value for key, value in catalog_document.items() if key != "entries"
    }
    observed_manifest_fields = {key: value for key, value in manifest.items() if key != "artifacts"}
    if not _documents_match(observed_manifest_fields, catalog_manifest_fields):
        raise CliOperationError("catalog manifest differs from catalog")
    if set(artifacts) != _expected_artifacts(validated, artifacts):
        raise CliOperationError("catalog artifact selection differs from manifest")
    return validated, checked


def _validate_against_state(
    observed: CatalogManifest,
    *,
    state: StateStore,
    raw_store: RawObjectStore,
    dependencies: CliDependencies,
) -> None:
    if observed.source != _SOURCE_NAME:
        raise CliOperationError("catalog source is not trusted")
    taxonomy = load_taxonomy(dependencies.taxonomy_path)
    if taxonomy.version != observed.taxonomy_version:
        raise CliOperationError("catalog taxonomy version differs from configured taxonomy")
    snapshot = state.catalog_snapshot(_SOURCE_NAME)
    for raw_hash in snapshot.raw_page_hashes:
        raw_store.verify(raw_hash)
    expected = build_catalog(
        snapshot.observations,
        taxonomy=taxonomy,
        context=CatalogBuildContext(
            source=snapshot.source,
            cursor_start=snapshot.cursor_start,
            cursor_end=snapshot.cursor_end,
            discovered_count=snapshot.discovered_count,
            pending_count=snapshot.pending_count,
            retry_count=snapshot.retry_count,
            dead_letter_count=snapshot.dead_letter_count,
            raw_page_hashes=snapshot.raw_page_hashes,
        ),
        classifier_version=observed.classifier_version,
        generated_at=observed.generated_at,
        classifier=dependencies.classifier,
        schema_version=observed.schema_version,
    )
    expected_document = expected.model_dump(mode="json", exclude_none=True)
    observed_document = observed.model_dump(mode="json", exclude_none=True)
    if not _documents_match(expected_document, observed_document):
        raise CliOperationError("catalog differs from durable state")


def _validate(path: Path, *, dependencies: CliDependencies) -> Mapping[str, object]:
    with _stores(path) as (workspace, raw_store, state):
        output = workspace / "catalog-output"
        if output.is_symlink() or not output.is_dir():
            raise CliOperationError("catalog output is unsafe or missing")
        if not (output / "manifest.json").is_file():
            raise CliOperationError("catalog manifest is missing")
        manifest, checked = _validate_documents(output)
        _validate_against_state(
            manifest,
            state=state,
            raw_store=raw_store,
            dependencies=dependencies,
        )
    return {"status": "valid", "artifacts_checked": checked, "entries": manifest.entry_count}


app = create_app()


def main() -> None:
    """Run the production CLI."""

    app()
