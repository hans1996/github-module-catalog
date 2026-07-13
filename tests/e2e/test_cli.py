"""End-to-end tests for the local catalog operations CLI."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import HttpUrl
from typer import Typer
from typer.testing import CliRunner

import github_module_catalog.cli as cli_module
from github_module_catalog.catalog import Classifier
from github_module_catalog.cli import (
    CliDependencies,
    CliOperationError,
    _read_yaml_object,
    create_app,
)
from github_module_catalog.models import (
    CapabilityAssertion,
    RepositoryIdentity,
    RepositoryObservation,
)
from github_module_catalog.source import (
    PageResult,
    RateLimitFacts,
    RepositoryFetchResult,
    RepositoryInventoryIdentity,
    RepositoryPage,
)
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import Taxonomy, classify_repository

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
RUNNER = CliRunner()


def test_yaml_alias_dag_is_rejected_before_object_construction() -> None:
    lines = ["root: &a [x, x]"]
    previous = "a"
    for index in range(10):
        current = chr(ord("b") + index)
        lines.append(f"{current}: &{current} [*{previous}, *{previous}]")
        previous = current
    payload = ("\n".join(lines) + "\n").encode()
    payload += b"#" + b"p" * (288 - len(payload))
    assert len(payload) == 289

    with pytest.raises(CliOperationError, match="aliases"):
        _read_yaml_object(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (("root: " + "[" * 101 + "x" + "]" * 101).encode(), "nesting"),
        (("root:\n" + "  - x\n" * 10_001).encode(), "node"),
    ],
)
def test_yaml_structure_bounds_apply_before_conversion(payload: bytes, message: str) -> None:
    with pytest.raises(CliOperationError, match=message):
        _read_yaml_object(payload)


def _credential_marker() -> str:
    return "test-only-auth-marker"


@dataclass
class FakeSource:
    outcome: RepositoryFetchResult

    def fetch_page(self, cursor: int, *, etag: str | None = None) -> RepositoryFetchResult:
        del cursor, etag
        return self.outcome


def _observation() -> RepositoryObservation:
    return RepositoryObservation(
        identity=RepositoryIdentity(repository_id=7),
        owner="octocat",
        name="module-catalog",
        full_name="octocat/module-catalog",
        html_url=HttpUrl("https://github.com/octocat/module-catalog"),
        description="A reusable CLI catalog",
        topics=("cli",),
        primary_language="Python",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 11, tzinfo=UTC),
        pushed_at=datetime(2026, 7, 12, tzinfo=UTC),
        observed_at=NOW,
        archived=False,
        disabled=False,
        fork=False,
        license_spdx=None,
        license_name=None,
    )


def _page() -> RepositoryPage:
    raw_bytes = json.dumps(
        [
            {
                "id": 7,
                "name": "module-catalog",
                "full_name": "octocat/module-catalog",
                "owner": {"login": "octocat", "id": 1},
                "html_url": "https://github.com/octocat/module-catalog",
                "description": "A reusable CLI catalog",
                "language": "Python",
                "created_at": "2026-07-01T00:00:00Z",
                "updated_at": "2026-07-11T00:00:00Z",
                "pushed_at": "2026-07-12T00:00:00Z",
                "archived": False,
                "disabled": False,
                "fork": False,
                "topics": ["cli"],
                "license": None,
            }
        ],
        separators=(",", ":"),
    ).encode()
    return RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        etag=None,
        next_url=None,
        next_cursor=7,
        rate_limit=RateLimitFacts(remaining=50),
        identities=(
            RepositoryInventoryIdentity(
                repository_id=7,
                name="module-catalog",
                full_name="octocat/module-catalog",
                owner_login="octocat",
                owner_id=1,
                html_url="https://github.com/octocat/module-catalog",
            ),
        ),
        observations=(_observation(),),
        observed_at=NOW,
    )


def _test_app(
    *,
    classifier: Classifier = classify_repository,
) -> tuple[Typer, list[str]]:
    received_tokens: list[str] = []

    def source_factory(token: str) -> FakeSource:
        received_tokens.append(token)
        return FakeSource(PageResult(_page()))

    dependencies = CliDependencies(
        source_factory=source_factory,
        now=lambda: NOW,
        classifier=classifier,
    )
    return create_app(dependencies), received_tokens


def _initialize_and_discover(app: Typer, workspace: Path) -> None:
    initialized = RUNNER.invoke(app, ["init", "--workspace", str(workspace)])
    assert initialized.exit_code == 0, initialized.output
    discovered = RUNNER.invoke(
        app,
        ["discover", "--workspace", str(workspace), "--max-pages", "1"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    assert discovered.exit_code == 0, discovered.output


def _write_catalog_documents(output: Path, document: dict[str, object]) -> None:
    (output / "catalog.json").write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    )
    (output / "catalog.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, default_flow_style=False, sort_keys=True)
    )


def _refresh_manifest_hashes(output: Path, **updates: object) -> None:
    manifest_path = output / "manifest.json"
    manifest = cast(dict[str, object], json.loads(manifest_path.read_text()))
    manifest.update(updates)
    artifacts = cast(dict[str, str], manifest["artifacts"])
    manifest["artifacts"] = {
        name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in artifacts
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    )


def _forge_permissive_license_catalog(output: Path) -> None:
    catalog = cast(dict[str, object], json.loads((output / "catalog.json").read_text()))
    entries = cast(list[dict[str, object]], catalog["entries"])
    repository = cast(dict[str, object], entries[0]["repository"])
    repository["license_spdx"] = "MIT"
    repository["license_name"] = "MIT License"
    repository["reuse_status"] = "safe_to_integrate"
    repository_input = dict(repository)
    repository_input.pop("reuse_status")
    observation_hash = RepositoryObservation.model_validate(repository_input).stable_hash()
    assertions = cast(list[dict[str, object]], entries[0]["assertions"])
    for assertion in assertions:
        assertion["license_spdx"] = "MIT"
        assertion["reuse_status"] = "safe_to_integrate"
        assertion["source_observation_hash"] = observation_hash
        evidence = cast(list[dict[str, object]], assertion["evidence"])
        for item in evidence:
            if item["source"] == "license":
                item["value"] = "MIT"
    catalog["source_hashes"] = [observation_hash]
    _write_catalog_documents(output, catalog)
    manifest_fields = {key: value for key, value in catalog.items() if key != "entries"}
    _refresh_manifest_hashes(output, **manifest_fields)


def _forge_unknown_source_empty_catalog(output: Path) -> None:
    catalog = cast(dict[str, object], json.loads((output / "catalog.json").read_text()))
    catalog.update(
        {
            "source": "attacker-controlled-source",
            "cursor_start": 0,
            "cursor_end": 0,
            "discovered_count": 0,
            "validated_observation_count": 0,
            "pending_count": 0,
            "retry_count": 0,
            "dead_letter_count": 0,
            "source_hashes": [],
            "raw_page_hashes": [],
            "classification_failure_repository_ids": [],
            "entries": [],
            "entry_count": 0,
            "capability_count": 0,
        }
    )
    _write_catalog_documents(output, catalog)
    manifest_fields = {key: value for key, value in catalog.items() if key != "entries"}
    _refresh_manifest_hashes(output, **manifest_fields)


def test_init_and_live_discover_use_injected_source_without_leaking_token(tmp_path: Path) -> None:
    app, received_tokens = _test_app()
    workspace = tmp_path / "workspace"

    initialized = RUNNER.invoke(app, ["init", "--workspace", str(workspace)])
    discovered = RUNNER.invoke(
        app,
        ["discover", "--workspace", str(workspace), "--max-pages", "1"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert initialized.exit_code == 0
    assert json.loads(initialized.stdout)["status"] == "initialized"
    assert (workspace / "data" / "state.sqlite3").is_file()
    assert discovered.exit_code == 0
    summary = json.loads(discovered.stdout)
    assert summary == {
        "cursor_end": 7,
        "observation_failures": 0,
        "observations_recorded": 1,
        "pages_committed": 1,
        "status": "completed",
    }
    assert received_tokens == [_credential_marker()]
    assert _credential_marker() not in initialized.output + discovered.output


def test_discover_requires_only_github_token_and_positive_bounded_pages(tmp_path: Path) -> None:
    app, received_tokens = _test_app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    missing = RUNNER.invoke(app, ["discover", "--workspace", str(workspace), "--max-pages", "1"])
    zero = RUNNER.invoke(
        app,
        ["discover", "--workspace", str(workspace), "--max-pages", "0"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    excessive = RUNNER.invoke(
        app,
        ["discover", "--workspace", str(workspace), "--max-pages", "1001"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert missing.exit_code != 0
    assert "GITHUB_TOKEN is required" in missing.output
    assert _credential_marker() not in missing.output
    assert zero.exit_code != 0
    assert excessive.exit_code != 0
    assert received_tokens == []


def test_status_classify_build_and_validate_use_durable_state(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)

    status_result = RUNNER.invoke(app, ["status", "--workspace", str(workspace)])
    classify_result = RUNNER.invoke(app, ["classify", "--workspace", str(workspace)])
    build_result = RUNNER.invoke(
        app,
        [
            "build",
            "--workspace",
            str(workspace),
            "--format",
            "json",
            "--format",
            "yaml",
            "--format",
            "markdown",
        ],
    )
    validate_result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert status_result.exit_code == 0
    assert json.loads(status_result.stdout) == {
        "cursor_end": 7,
        "dead_letter": 0,
        "discovered": 1,
        "observations": 1,
        "pending": 0,
        "retry": 0,
    }
    assert classify_result.exit_code == 0
    assert json.loads(classify_result.stdout) == {
        "capabilities": 1,
        "classification_failures": 0,
        "entries": 1,
        "writes": False,
    }
    assert build_result.exit_code == 0
    output = workspace / "catalog-output"
    assert json.loads(build_result.stdout)["output"] == str(output.resolve())
    assert validate_result.exit_code == 0
    assert json.loads(validate_result.stdout)["status"] == "valid"
    catalog_document = json.loads((output / "catalog.json").read_text())
    assert catalog_document["source"] == "github"
    assert catalog_document == yaml.safe_load((output / "catalog.yaml").read_text())
    assert "discovery_only" in (output / "catalog.json").read_text()


def test_classification_failure_is_counted_without_aborting_dry_run(tmp_path: Path) -> None:
    def failing_classifier(
        observation: RepositoryObservation,
        taxonomy: Taxonomy,
        *,
        classifier_version: str,
    ) -> tuple[CapabilityAssertion, ...]:
        del observation, taxonomy, classifier_version
        raise RuntimeError("untrusted classifier detail")

    app, _ = _test_app(classifier=failing_classifier)
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)

    result = RUNNER.invoke(app, ["classify", "--workspace", str(workspace)])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["classification_failures"] == 1
    assert "untrusted classifier detail" not in result.output
    assert not (workspace / "catalog-output").exists()


def test_commands_fail_safely_for_missing_token_unsafe_paths_and_corruption(
    tmp_path: Path,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0

    outside = tmp_path / "outside"
    outside.mkdir()
    unsafe_workspace = tmp_path / "unsafe-workspace"
    unsafe_workspace.symlink_to(outside, target_is_directory=True)
    unsafe = RUNNER.invoke(app, ["init", "--workspace", str(unsafe_workspace)])

    output = workspace / "catalog-output"
    for path in sorted(output.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    output.rmdir()
    output.symlink_to(outside, target_is_directory=True)
    unsafe_output = RUNNER.invoke(app, ["build", "--workspace", str(workspace)])

    output.unlink()
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    (workspace / "catalog-output" / "catalog.json").write_text("{}\n")
    corrupt = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    invalid_workspace = tmp_path / "invalid-state"
    (invalid_workspace / "data").mkdir(parents=True)
    (invalid_workspace / "data" / "state.sqlite3").write_bytes(b"not sqlite")
    invalid_state = RUNNER.invoke(app, ["status", "--workspace", str(invalid_workspace)])

    assert unsafe.exit_code != 0
    assert unsafe_output.exit_code != 0
    assert corrupt.exit_code != 0
    assert invalid_state.exit_code != 0
    assert _credential_marker() not in (
        unsafe.output + unsafe_output.output + corrupt.output + invalid_state.output
    )


def test_validate_rejects_manifest_hash_or_schema_corruption(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    manifest_path = workspace / "catalog-output" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = ""
    manifest_path.write_text(json.dumps(manifest))

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert "valid" not in result.output.casefold()


def test_validate_rejects_artifact_swap_at_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n")
    real_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and path == "catalog.json" and dir_fd is not None and flags & os.O_NOFOLLOW:
            swapped = True
            os.rename(
                "catalog.json",
                "stolen-catalog.json",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.symlink(outside, "catalog.json", dir_fd=dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert swapped is True
    assert result.exit_code != 0
    assert outside.read_text() == "{}\n"


@pytest.mark.parametrize("tamper", ["counts", "reuse_status"])
def test_validate_recomputes_catalog_semantics_after_hash_consistent_tampering(
    tmp_path: Path, tamper: str
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    output = workspace / "catalog-output"
    catalog = cast(dict[str, object], json.loads((output / "catalog.json").read_text()))

    if tamper == "counts":
        catalog["entry_count"] = 99
        catalog["capability_count"] = 99
        manifest_updates: dict[str, object] = {"entry_count": 99, "capability_count": 99}
    else:
        entries = cast(list[dict[str, object]], catalog["entries"])
        repository = cast(dict[str, object], entries[0]["repository"])
        repository["reuse_status"] = "safe_to_integrate"
        manifest_updates = {}
    _write_catalog_documents(output, catalog)
    _refresh_manifest_hashes(output, **manifest_updates)

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_validate_rejects_a_consistent_license_forgery_that_differs_from_state(
    tmp_path: Path,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    output = workspace / "catalog-output"
    _forge_permissive_license_catalog(output)

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_validate_rejects_an_empty_catalog_from_an_unknown_source(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    built = RUNNER.invoke(
        app,
        [
            "build",
            "--workspace",
            str(workspace),
            "--format",
            "json",
            "--format",
            "yaml",
        ],
    )
    assert built.exit_code == 0
    output = workspace / "catalog-output"
    _forge_unknown_source_empty_catalog(output)

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_validate_rejects_untrusted_schema_version_even_when_documents_match(
    tmp_path: Path,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    output = workspace / "catalog-output"
    catalog = cast(dict[str, object], json.loads((output / "catalog.json").read_text()))
    catalog["schema_version"] = "attacker-v9"
    _write_catalog_documents(output, catalog)
    _refresh_manifest_hashes(output, schema_version="attacker-v9")

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_validate_requires_latest_matching_publication_ledger_record(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    state_path = workspace / "data" / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        record = connection.execute(
            "SELECT source, generated_at, schema_version, taxonomy_version, "
            "classifier_version, manifest_sha256, artifact_manifest_sha256 "
            "FROM catalog_publications"
        ).fetchone()
        assert record is not None
        assert record[:5] == (
            "github",
            "2026-07-13T12:00:00Z",
            "1.0.0",
            "1.0.0",
            "rules-v1",
        )
        assert all(len(str(item)) == 64 for item in record[5:])
        connection.execute("DELETE FROM catalog_publications")

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_validate_rejects_tampered_latest_publication_ledger_record(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    with sqlite3.connect(workspace / "data" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE catalog_publications SET artifact_manifest_sha256 = ?",
            ("0" * 64,),
        )

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_failed_publication_never_creates_a_ledger_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)

    def fail_publication(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(cli_module, "publish_catalog", fail_publication)

    result = RUNNER.invoke(app, ["build", "--workspace", str(workspace)])

    assert result.exit_code != 0
    with sqlite3.connect(workspace / "data" / "state.sqlite3") as connection:
        count = connection.execute("SELECT COUNT(*) FROM catalog_publications").fetchone()
    assert count == (0,)


@pytest.mark.parametrize("state_mode", ["missing", "corrupt"])
def test_validate_fails_closed_without_valid_durable_state(tmp_path: Path, state_mode: str) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    state_path = workspace / "data" / "state.sqlite3"
    if state_mode == "missing":
        state_path.unlink()
    else:
        state_path.write_bytes(b"not sqlite")

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_build_rejects_state_observation_not_derivable_from_bound_raw_page(
    tmp_path: Path,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    state_path = workspace / "data" / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        row = connection.execute(
            "SELECT id, observation_json FROM repository_observations WHERE repository_id = 7"
        ).fetchone()
        forged_document = cast(dict[str, object], json.loads(str(row[1])))
        forged_document["license_spdx"] = "MIT"
        forged_document["license_name"] = "MIT License"
        forged = RepositoryObservation.model_validate(forged_document)
        connection.execute(
            "UPDATE repository_observations SET observation_hash = ?, observation_json = ? "
            "WHERE id = ?",
            (forged.stable_hash(), forged.stable_json(), int(row[0])),
        )

    result = RUNNER.invoke(app, ["build", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert not (workspace / "catalog-output").exists()


def test_init_rejects_a_workspace_with_symlinked_state_storage(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "data").symlink_to(outside, target_is_directory=True)

    result = RUNNER.invoke(app, ["init", "--workspace", str(workspace)])

    assert result.exit_code != 0
    assert list(outside.iterdir()) == []


def test_workspace_swap_between_validation_and_store_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    moved_workspace = tmp_path / "moved-workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    swapped = False

    def swapping_raw_store(
        workspace_root: Path, *, workspace_fd: int | None = None
    ) -> RawObjectStore:
        nonlocal swapped
        if not swapped:
            swapped = True
            workspace.rename(moved_workspace)
            workspace.symlink_to(outside, target_is_directory=True)
        if workspace_fd is None:
            return RawObjectStore(workspace_root)
        store = RawObjectStore(workspace_root, workspace_fd=workspace_fd)
        store.write(b"pinned raw probe")
        return store

    monkeypatch.setattr(cli_module, "RawObjectStore", swapping_raw_store)

    result = RUNNER.invoke(app, ["init", "--workspace", str(workspace)])

    assert swapped is True
    assert result.exit_code != 0
    assert marker.read_text() == "keep"
    assert not (outside / "data" / "state.sqlite3").exists()
    assert not (outside / "data" / "raw").exists()
    assert any((moved_workspace / "data" / "raw" / "sha256").rglob("*.json"))


@pytest.mark.parametrize("missing_key", ["schema_version", "modules/cli.md"])
def test_validate_rejects_incomplete_manifest(tmp_path: Path, missing_key: str) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    assert RUNNER.invoke(app, ["build", "--workspace", str(workspace)]).exit_code == 0
    manifest_path = workspace / "catalog-output" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if missing_key == "schema_version":
        manifest.pop(missing_key)
    else:
        manifest["artifacts"].pop(missing_key)
    manifest_path.write_text(json.dumps(manifest))

    result = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])

    assert result.exit_code != 0


@pytest.mark.parametrize(
    ("formats", "expected_files", "validation_succeeds"),
    [
        (["json"], {"catalog.json", "manifest.json"}, True),
        (["yaml"], {"catalog.yaml", "manifest.json"}, True),
        (
            ["markdown", "json"],
            {"README.md", "catalog.json", "manifest.json", "modules/cli.md"},
            True,
        ),
    ],
)
def test_build_publishes_exact_selected_format_subset(
    tmp_path: Path,
    formats: list[str],
    expected_files: set[str],
    validation_succeeds: bool,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    arguments = ["build", "--workspace", str(workspace)]
    for output_format in formats:
        arguments.extend(("--format", output_format))

    built = RUNNER.invoke(app, arguments)

    assert built.exit_code == 0
    output = workspace / "catalog-output"
    actual_files = {
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    }
    assert actual_files == expected_files
    summary = json.loads(built.stdout)
    assert summary["formats"] == sorted(formats)
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["artifacts"]) == expected_files - {"manifest.json"}
    validated = RUNNER.invoke(app, ["validate", "--workspace", str(workspace)])
    assert (validated.exit_code == 0) is validation_succeeds


def test_build_rejects_markdown_only_before_publication(tmp_path: Path) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)

    result = RUNNER.invoke(app, ["build", "--workspace", str(workspace), "--format", "markdown"])

    assert result.exit_code != 0
    assert not (workspace / "catalog-output").exists()


def test_rebuild_rejects_conflicting_format_set_before_replacing_valid_output(
    tmp_path: Path,
) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    first = RUNNER.invoke(
        app,
        ["build", "--workspace", str(workspace), "--format", "json"],
    )
    assert first.exit_code == 0
    output = workspace / "catalog-output"
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    conflicting = RUNNER.invoke(
        app,
        ["build", "--workspace", str(workspace), "--format", "yaml"],
    )

    after = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    assert conflicting.exit_code != 0
    assert after == before
    assert RUNNER.invoke(app, ["validate", "--workspace", str(workspace)]).exit_code == 0


@pytest.mark.parametrize("formats", [["xml"], ["json", "json"]])
def test_build_rejects_unknown_or_duplicate_formats(tmp_path: Path, formats: list[str]) -> None:
    app, _ = _test_app()
    workspace = tmp_path / "workspace"
    _initialize_and_discover(app, workspace)
    arguments = ["build", "--workspace", str(workspace)]
    for output_format in formats:
        arguments.extend(("--format", output_format))

    result = RUNNER.invoke(app, arguments)

    assert result.exit_code != 0
