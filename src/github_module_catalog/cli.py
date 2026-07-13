"""Safe command-line operations for a bounded local catalog workspace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Protocol, cast, runtime_checkable

import typer
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from github_module_catalog.catalog import (
    CatalogBuildContext,
    Classifier,
    build_catalog,
)
from github_module_catalog.exporters import (
    CatalogFormat,
    publish_catalog,
    render_publication_manifest,
)
from github_module_catalog.github import GitHubRepositorySource, parse_github_inventory
from github_module_catalog.models import CatalogManifest
from github_module_catalog.safeio import (
    UnsafeOutputPathError,
    file_identity,
    list_regular_files_at,
    open_directory,
    open_directory_at,
    read_regular_file_at,
)
from github_module_catalog.scanner import DiscoveryScanner, ScanStatus
from github_module_catalog.source import RepositorySource
from github_module_catalog.state import CatalogStateSnapshot, StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import classify_repository, load_taxonomy

_SOURCE_NAME = "github"
_SCHEMA_VERSION = "1.0.0"
_CLASSIFIER_VERSION = "rules-v1"
_MAX_PAGES = 1_000
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
_MAX_ARTIFACT_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_ARTIFACTS = 10_000
_MAX_YAML_NODES = 10_000
_MAX_YAML_DEPTH = 100
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

WorkspaceOption = Annotated[Path, typer.Option("--workspace", help="Local catalog workspace.")]


class CliOperationError(RuntimeError):
    """A safe, user-facing operational failure."""


@runtime_checkable
class _Closable(Protocol):
    def close(self) -> None: ...


def _default_source_factory(token: str) -> RepositorySource:
    return GitHubRepositorySource(token=token)


def _default_taxonomy_path() -> Traversable:
    return files("github_module_catalog").joinpath("data", "taxonomy.yaml")


@dataclass(frozen=True, slots=True)
class CliDependencies:
    """Injectable boundaries used by command-level tests and production."""

    source_factory: Callable[[str], RepositorySource] = _default_source_factory
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    classifier: Classifier = classify_repository
    taxonomy_path: str | Path | Traversable = field(default_factory=_default_taxonomy_path)


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


@dataclass(frozen=True, slots=True)
class _PinnedStores:
    """Command-scoped stores anchored to one immutable workspace descriptor."""

    workspace: Path
    workspace_fd: int
    raw_store: RawObjectStore
    state: StateStore


def _verify_pinned_workspace(
    workspace: Path, workspace_fd: int, expected_identity: tuple[int, int, int]
) -> None:
    try:
        observed = os.stat(workspace, follow_symlinks=False)
    except OSError:
        raise CliOperationError("workspace changed during command execution") from None
    if (
        not stat.S_ISDIR(observed.st_mode)
        or file_identity(observed) != expected_identity
        or file_identity(os.fstat(workspace_fd)) != expected_identity
    ):
        raise CliOperationError("workspace changed during command execution")


@contextmanager
def _stores(path: Path, *, create: bool = False) -> Iterator[_PinnedStores]:
    workspace = _safe_workspace(path, create=create)
    state_path = _state_path(workspace)
    workspace_fd = open_directory(workspace)
    workspace_identity = file_identity(os.fstat(workspace_fd))
    raw_store: RawObjectStore | None = None
    state: StateStore | None = None
    try:
        _verify_pinned_workspace(workspace, workspace_fd, workspace_identity)
        raw_store = RawObjectStore(workspace, workspace_fd=workspace_fd)
        state = StateStore(
            state_path,
            raw_store,
            workspace_fd=workspace_fd,
            create_database=create,
        )
        _verify_pinned_workspace(workspace, workspace_fd, workspace_identity)
        yield _PinnedStores(workspace, workspace_fd, raw_store, state)
        _verify_pinned_workspace(workspace, workspace_fd, workspace_identity)
    finally:
        if state is not None:
            state.close()
        if raw_store is not None:
            raw_store.close()
        os.close(workspace_fd)


def _initialize(path: Path) -> Mapping[str, object]:
    with _stores(path, create=True) as stores:
        return {"status": "initialized", "workspace": str(stores.workspace)}


def _github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token is None or not token.strip():
        raise CliOperationError("GITHUB_TOKEN is required for discovery")
    return token


def _discover(path: Path, *, max_pages: int, dependencies: CliDependencies) -> Mapping[str, object]:
    token = _github_token()
    with _stores(path) as stores:
        source = dependencies.source_factory(token)
        try:
            outcome = DiscoveryScanner(
                source=source,
                raw_store=stores.raw_store,
                state=stores.state,
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
    with _stores(path) as stores:
        return _snapshot_summary(stores.state.catalog_snapshot(_SOURCE_NAME))


def _manifest(
    state: StateStore,
    dependencies: CliDependencies,
    *,
    raw_store: RawObjectStore | None = None,
    generated_at: datetime | None = None,
) -> CatalogManifest:
    taxonomy = load_taxonomy(dependencies.taxonomy_path)
    snapshot = state.catalog_snapshot(_SOURCE_NAME)
    if raw_store is not None:
        _verify_snapshot_provenance(snapshot, raw_store)
    return build_catalog(
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
        classifier_version=_CLASSIFIER_VERSION,
        generated_at=generated_at,
        classifier=dependencies.classifier,
        schema_version=_SCHEMA_VERSION,
    )


def _classify(path: Path, *, dependencies: CliDependencies) -> Mapping[str, object]:
    with _stores(path) as stores:
        manifest = _manifest(stores.state, dependencies)
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
    with _stores(path) as stores:
        generated_at = dependencies.now()
        manifest = _manifest(
            stores.state,
            dependencies,
            raw_store=stores.raw_store,
            generated_at=generated_at,
        )
        output = stores.workspace / "catalog-output"
        expected_artifact_manifest_sha256 = hashlib.sha256(
            render_publication_manifest(manifest, formats=selected)
        ).hexdigest()
        stores.state.ensure_catalog_publication_compatible(
            manifest,
            artifact_manifest_sha256=expected_artifact_manifest_sha256,
        )
        artifacts = publish_catalog(
            manifest,
            output,
            formats=selected,
            trusted_parent_fd=stores.workspace_fd,
        )
        output_fd = open_directory_at(stores.workspace_fd, "catalog-output")
        try:
            manifest_bytes = read_regular_file_at(
                output_fd,
                "manifest.json",
                max_bytes=_MAX_ARTIFACT_BYTES,
            )
        finally:
            os.close(output_fd)
        artifact_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if artifact_manifest_sha256 != expected_artifact_manifest_sha256:
            raise CliOperationError("published artifact manifest differs from planned output")
        stores.state.record_catalog_publication(
            manifest,
            artifact_manifest_sha256=artifact_manifest_sha256,
            published_at=dependencies.now(),
        )
    return {
        "artifacts": len(artifacts),
        "entries": manifest.entry_count,
        "formats": sorted(item.value for item in selected),
        "output": str(output.resolve()),
    }


def _read_json_object(content: bytes) -> dict[str, object]:
    try:
        document = json.loads(content.decode("utf-8"))
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
    if len(artifacts) > _MAX_ARTIFACTS:
        raise CliOperationError("artifact manifest exceeds the entry limit")
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


def _artifact_contents(output_fd: int, artifacts: dict[str, str]) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    total_bytes = 0
    for raw_name, raw_digest in artifacts.items():
        if _SHA256.fullmatch(raw_digest) is None:
            raise CliOperationError("artifact manifest contains an unsafe entry")
        content = read_regular_file_at(
            output_fd,
            raw_name,
            max_bytes=_MAX_ARTIFACT_BYTES,
        )
        total_bytes += len(content)
        if total_bytes > _MAX_ARTIFACT_TOTAL_BYTES:
            raise CliOperationError("catalog artifacts exceed the total size limit")
        observed = hashlib.sha256(content).hexdigest()
        if observed != raw_digest:
            raise CliOperationError("catalog artifact hash differs")
        contents[raw_name] = content
    actual_files = list_regular_files_at(output_fd)
    if actual_files != set(artifacts) | {"manifest.json"}:
        raise CliOperationError("catalog artifact list differs from output")
    return contents


def _read_yaml_object(content: bytes) -> dict[str, object]:
    try:
        decoded = content.decode("utf-8")
        _validate_yaml_structure(decoded)
        document = yaml.safe_load(decoded)
    except (UnicodeDecodeError, yaml.YAMLError):
        raise CliOperationError("catalog YAML could not be checked") from None
    if not isinstance(document, dict):
        raise CliOperationError("catalog YAML must contain an object")
    return cast(dict[str, object], document)


def _validate_yaml_structure(document: str) -> None:
    node_count = 0
    depth = 0
    for event in yaml.parse(document):
        if isinstance(event, yaml.events.AliasEvent):
            raise CliOperationError("catalog YAML aliases are not allowed")
        if isinstance(
            event,
            (
                yaml.events.MappingStartEvent,
                yaml.events.SequenceStartEvent,
                yaml.events.ScalarEvent,
            ),
        ):
            node_count += 1
            if node_count > _MAX_YAML_NODES:
                raise CliOperationError("catalog YAML exceeds the node limit")
        if isinstance(
            event,
            (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent),
        ):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise CliOperationError("catalog YAML exceeds the nesting limit")
        elif isinstance(
            event,
            (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent),
        ):
            depth -= 1


def _machine_catalogs(
    contents: dict[str, bytes], artifacts: dict[str, str]
) -> tuple[dict[str, object], ...]:
    documents: list[dict[str, object]] = []
    if "catalog.json" in artifacts:
        documents.append(_read_json_object(contents["catalog.json"]))
    if "catalog.yaml" in artifacts:
        documents.append(_read_yaml_object(contents["catalog.yaml"]))
    if not documents:
        raise CliOperationError("validation requires a JSON or YAML catalog")
    if len(documents) == 2 and not _documents_match(documents[0], documents[1]):
        raise CliOperationError("catalog JSON and YAML differ")
    return tuple(documents)


def _validate_documents(output_fd: int) -> tuple[CatalogManifest, int, bytes]:
    manifest_bytes = read_regular_file_at(
        output_fd,
        "manifest.json",
        max_bytes=_MAX_ARTIFACT_BYTES,
    )
    manifest = _read_json_object(manifest_bytes)
    artifacts = _artifact_mapping(manifest)
    contents = _artifact_contents(output_fd, artifacts)
    documents = _machine_catalogs(contents, artifacts)
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
    return validated, len(contents), manifest_bytes


def _validate_against_state(
    observed: CatalogManifest,
    *,
    state: StateStore,
    raw_store: RawObjectStore,
    dependencies: CliDependencies,
    artifact_manifest_sha256: str,
) -> None:
    if observed.source != _SOURCE_NAME:
        raise CliOperationError("catalog source is not trusted")
    if observed.schema_version != _SCHEMA_VERSION:
        raise CliOperationError("catalog schema version is not trusted")
    if observed.classifier_version != _CLASSIFIER_VERSION:
        raise CliOperationError("catalog classifier version is not trusted")
    taxonomy = load_taxonomy(dependencies.taxonomy_path)
    if taxonomy.version != observed.taxonomy_version:
        raise CliOperationError("catalog taxonomy version differs from configured taxonomy")
    publication = state.latest_catalog_publication(_SOURCE_NAME)
    if (
        publication is None
        or publication.manifest != observed
        or publication.manifest_sha256 != observed.stable_hash()
        or publication.artifact_manifest_sha256 != artifact_manifest_sha256
    ):
        raise CliOperationError("catalog does not match the latest publication ledger record")
    snapshot = state.catalog_snapshot(_SOURCE_NAME)
    _verify_snapshot_provenance(snapshot, raw_store)
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
        classifier_version=_CLASSIFIER_VERSION,
        generated_at=observed.generated_at,
        classifier=dependencies.classifier,
        schema_version=_SCHEMA_VERSION,
    )
    expected_document = expected.model_dump(mode="json", exclude_none=True)
    observed_document = observed.model_dump(mode="json", exclude_none=True)
    if not _documents_match(expected_document, observed_document):
        raise CliOperationError("catalog differs from durable state")


def _verify_snapshot_provenance(snapshot: CatalogStateSnapshot, raw_store: RawObjectStore) -> None:
    observations = {
        (item.identity.repository_id, item.stable_hash()): item for item in snapshot.observations
    }
    bindings_by_page = {
        page.page_id: tuple(
            binding for binding in snapshot.observation_bindings if binding.page_id == page.page_id
        )
        for page in snapshot.pages
    }
    bound_observations: set[tuple[int, str]] = set()
    for page in snapshot.pages:
        raw_bytes = raw_store.read(page.raw_sha256)
        parsed = parse_github_inventory(raw_bytes, observed_at=page.observed_at)
        parsed_ids = tuple(sorted(identity.repository_id for identity in parsed.identities))
        if parsed_ids != page.repository_ids:
            raise CliOperationError("raw repository identities differ from durable page bindings")
        derived = {item.identity.repository_id: item for item in parsed.observations}
        for binding in bindings_by_page[page.page_id]:
            item = derived.get(binding.repository_id)
            key = (binding.repository_id, binding.observation_hash)
            if (
                binding.raw_sha256 != page.raw_sha256
                or binding.observed_at != page.observed_at
                or item is None
                or item.stable_hash() != binding.observation_hash
                or observations.get(key) != item
            ):
                raise CliOperationError("observation is not derivable from its bound raw page")
            bound_observations.add(key)
    if set(observations) != bound_observations:
        raise CliOperationError("catalog observations are missing raw page provenance")


def _validate(path: Path, *, dependencies: CliDependencies) -> Mapping[str, object]:
    with _stores(path) as stores:
        output_fd = open_directory_at(stores.workspace_fd, "catalog-output")
        try:
            manifest, checked, manifest_bytes = _validate_documents(output_fd)
            _validate_against_state(
                manifest,
                state=stores.state,
                raw_store=stores.raw_store,
                dependencies=dependencies,
                artifact_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        finally:
            os.close(output_fd)
    return {"status": "valid", "artifacts_checked": checked, "entries": manifest.entry_count}


app = create_app()


def main() -> None:
    """Run the production CLI."""

    app()
