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

from github_module_catalog.catalog import Classifier, build_catalog_from_state
from github_module_catalog.exporters import UnsafeOutputPathError, publish_catalog
from github_module_catalog.github import GitHubRepositorySource
from github_module_catalog.models import CatalogManifest
from github_module_catalog.scanner import DiscoveryScanner, ScanStatus
from github_module_catalog.source import RepositorySource
from github_module_catalog.state import CatalogStateSnapshot, StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import classify_repository, load_taxonomy

_SOURCE_NAME = "github-public-repositories"
_MAX_PAGES = 1_000
_FORMATS = frozenset({"json", "yaml", "markdown"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ARTIFACTS = frozenset({"README.md", "catalog.json", "catalog.yaml"})

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
        _command(lambda: _validate(workspace))

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


def _validated_formats(formats: list[str] | None) -> tuple[str, ...]:
    selected = tuple(formats or sorted(_FORMATS))
    if len(selected) != len(set(selected)) or any(item not in _FORMATS for item in selected):
        raise CliOperationError("formats must be unique: json, yaml, markdown")
    return selected


def _build(
    path: Path, *, formats: list[str] | None, dependencies: CliDependencies
) -> Mapping[str, object]:
    selected = _validated_formats(formats)
    with _stores(path) as (workspace, _raw_store, state):
        manifest = _manifest(state, dependencies)
        output = workspace / "catalog-output"
        artifacts = publish_catalog(manifest, output)
    return {
        "artifacts": len(artifacts),
        "entries": manifest.entry_count,
        "formats": list(selected),
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


def _catalog_schema_input(document: dict[str, object]) -> dict[str, object]:
    schema_input = dict(document)
    schema_input.pop("entry_count", None)
    schema_input.pop("capability_count", None)
    entries = schema_input.get("entries")
    if not isinstance(entries, list):
        raise CliOperationError("catalog entries could not be checked")
    clean_entries: list[object] = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("repository"), dict):
            raise CliOperationError("catalog entry could not be checked")
        clean_item = dict(item)
        repository = dict(cast(dict[object, object], item["repository"]))
        repository.pop("reuse_status", None)
        clean_item["repository"] = repository
        clean_entries.append(clean_item)
    schema_input["entries"] = clean_entries
    return schema_input


def _artifact_hashes(output: Path, manifest: dict[str, object], catalog: CatalogManifest) -> int:
    artifacts = manifest.get("artifacts")
    module_artifacts = {
        f"modules/{assertion.capability_id}.md"
        for entry in catalog.entries
        for assertion in entry.assertions
    }
    required_artifacts = _REQUIRED_ARTIFACTS | module_artifacts
    if not isinstance(artifacts, dict) or not required_artifacts.issubset(artifacts):
        raise CliOperationError("required catalog artifacts are missing")
    checked = 0
    for raw_name, raw_digest in artifacts.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise CliOperationError("artifact manifest could not be checked")
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
    return checked


def _validate_documents(output: Path) -> tuple[CatalogManifest, int]:
    catalog_json = _read_json_object(output / "catalog.json")
    try:
        catalog_yaml = yaml.safe_load((output / "catalog.yaml").read_text(encoding="utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise CliOperationError("catalog YAML could not be checked") from None
    if catalog_yaml != catalog_json:
        raise CliOperationError("catalog JSON and YAML differ")
    try:
        validated = CatalogManifest.model_validate(_catalog_schema_input(catalog_json))
    except ValidationError:
        raise CliOperationError("catalog schema check failed") from None
    manifest = _read_json_object(output / "manifest.json")
    catalog_manifest_fields = {
        key: value for key, value in catalog_json.items() if key != "entries"
    }
    observed_manifest_fields = {key: value for key, value in manifest.items() if key != "artifacts"}
    if observed_manifest_fields != catalog_manifest_fields:
        raise CliOperationError("catalog manifest differs from catalog")
    return validated, _artifact_hashes(output, manifest, validated)


def _validate(path: Path) -> Mapping[str, object]:
    workspace = _safe_workspace(path)
    if not _state_path(workspace).is_file():
        raise CliOperationError("workspace is not initialized")
    output = workspace / "catalog-output"
    if output.is_symlink() or not output.is_dir():
        raise CliOperationError("catalog output is unsafe or missing")
    for name in ("manifest.json", *_REQUIRED_ARTIFACTS):
        if not (output / name).is_file():
            raise CliOperationError("required catalog artifacts are missing")
    manifest, checked = _validate_documents(output)
    return {"status": "valid", "artifacts_checked": checked, "entries": manifest.entry_count}


app = create_app()


def main() -> None:
    """Run the production CLI."""

    app()
