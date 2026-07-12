"""Tests for validated catalog construction and deterministic publication."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import HttpUrl

from github_module_catalog.catalog import (
    CatalogBuildContext,
    build_catalog,
    build_catalog_from_state,
)
from github_module_catalog.exporters import (
    UnsafeOutputPathError,
    publish_catalog,
    render_catalog_json,
    render_catalog_yaml,
    render_readme,
)
from github_module_catalog.models import (
    CapabilityAssertion,
    CatalogManifest,
    RepositoryIdentity,
    RepositoryObservation,
    ReuseStatus,
)
from github_module_catalog.state import StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import Taxonomy, classify_repository, load_taxonomy

NOW = datetime(2026, 7, 13, tzinfo=UTC)
TAXONOMY_PATH = Path(__file__).parents[2] / "config" / "taxonomy.yaml"


def _observation(
    repository_id: int,
    *,
    topic: str = "cli",
    license_spdx: str | None = "MIT",
    description: str = "A useful CLI",
) -> RepositoryObservation:
    name = f"repo-{repository_id}"
    return RepositoryObservation(
        identity=RepositoryIdentity(repository_id=repository_id),
        owner="octocat",
        name=name,
        full_name=f"octocat/{name}",
        html_url=HttpUrl(f"https://github.com/octocat/{name}"),
        description=description,
        topics=(topic,),
        primary_language="Python",
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        license_spdx=license_spdx,
        license_name="License" if license_spdx else None,
    )


def _context() -> CatalogBuildContext:
    return CatalogBuildContext(
        source="github-public-repositories",
        cursor_start=0,
        cursor_end=9,
        discovered_count=4,
        pending_count=2,
        retry_count=1,
        dead_letter_count=1,
        raw_page_hashes=("a" * 64, "b" * 64),
    )


def _manifest(
    observations: tuple[RepositoryObservation, ...] | None = None,
    *,
    generated_at: datetime | None = None,
) -> CatalogManifest:
    return build_catalog(
        observations
        or (_observation(9, topic="auth", description="Auth service"), _observation(2)),
        taxonomy=load_taxonomy(TAXONOMY_PATH),
        context=_context(),
        classifier_version="rules-v1",
        generated_at=generated_at,
    )


def test_builder_accepts_only_validated_immutable_observations_and_state_round_trip(
    tmp_path: Path,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    with pytest.raises(TypeError, match="RepositoryObservation"):
        build_catalog(({"id": 7},), taxonomy=taxonomy, context=_context())  # type: ignore[arg-type]

    raw_store = RawObjectStore(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3", raw_store)
    observation = _observation(7)
    state.record_repository_observation(observation)

    manifest = build_catalog_from_state(state, taxonomy=taxonomy, context=_context())

    assert manifest.entries[0].repository == observation
    assert manifest.source_hashes == (observation.stable_hash(),)


def test_classifier_failure_is_isolated_and_manifest_remains_truthful() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)

    def classifier(
        observation: RepositoryObservation,
        configured_taxonomy: Taxonomy,
        *,
        classifier_version: str,
    ) -> tuple[CapabilityAssertion, ...]:
        if observation.identity.repository_id == 2:
            raise RuntimeError("classification failed")
        return classify_repository(
            observation,
            configured_taxonomy,
            classifier_version=classifier_version,
        )

    manifest = build_catalog(
        (_observation(9, topic="auth"), _observation(2)),
        taxonomy=taxonomy,
        context=_context(),
        classifier=classifier,
    )

    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [2, 9]
    assert manifest.entries[0].assertions == ()
    assert manifest.classification_failure_repository_ids == (2,)
    assert manifest.validated_observation_count == 2


def test_json_yaml_and_markdown_have_equivalent_sorted_catalog_entries() -> None:
    manifest = _manifest()
    json_document = json.loads(render_catalog_json(manifest))
    yaml_document = yaml.safe_load(render_catalog_yaml(manifest))
    markdown = render_readme(manifest)

    assert json_document == yaml_document
    assert [
        entry["repository"]["identity"]["repository_id"] for entry in json_document["entries"]
    ] == [2, 9]
    assert [item["capability_id"] for item in json_document["entries"][1]["assertions"]] == ["auth"]
    assert markdown.index("octocat/repo-2") < markdown.index("octocat/repo-9")
    assert "`auth`" in markdown and "`cli`" in markdown


def test_repeated_publication_is_byte_identical_and_build_time_is_opt_in(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    output = tmp_path / "output"
    publish_catalog(manifest, output)
    first = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }
    publish_catalog(manifest, output)
    second = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    assert first == second
    assert set(first) == {
        Path("README.md"),
        Path("catalog.json"),
        Path("catalog.yaml"),
        Path("manifest.json"),
        Path("modules/auth.md"),
        Path("modules/cli.md"),
    }
    assert "generated_at" not in json.loads(first[Path("manifest.json")])
    timestamped = _manifest(generated_at=NOW)
    assert json.loads(render_catalog_json(timestamped))["generated_at"] == "2026-07-13T00:00:00Z"


def test_manifest_reports_coverage_counts_versions_and_source_hashes() -> None:
    manifest = _manifest()

    assert manifest.source == "github-public-repositories"
    assert (manifest.cursor_start, manifest.cursor_end) == (0, 9)
    assert (manifest.discovered_count, manifest.validated_observation_count) == (4, 2)
    assert (manifest.pending_count, manifest.retry_count, manifest.dead_letter_count) == (2, 1, 1)
    assert manifest.schema_version == "1.0.0"
    assert manifest.taxonomy_version == "1.0.0"
    assert manifest.classifier_version == "rules-v1"
    assert manifest.raw_page_hashes == ("a" * 64, "b" * 64)
    assert manifest.source_hashes == tuple(
        sorted(entry.repository.stable_hash() for entry in manifest.entries)
    )
    assert manifest.coverage_complete is False


@pytest.mark.parametrize("license_spdx", [None, "NOASSERTION"])
def test_unknown_license_never_crosses_the_integration_gate(license_spdx: str | None) -> None:
    manifest = _manifest((_observation(7, license_spdx=license_spdx),))
    entry = manifest.entries[0]

    assert entry.repository.reuse_status == ReuseStatus.DISCOVERY_ONLY
    assert {assertion.reuse_status for assertion in entry.assertions} == {
        ReuseStatus.DISCOVERY_ONLY
    }
    assert "safe_to_integrate" not in render_readme(manifest)


def test_markdown_uses_validated_https_links_and_does_not_render_untrusted_description() -> None:
    description = "Tool | [click](javascript:alert(1))\nnext line"
    manifest = _manifest((_observation(7, description=description),))
    markdown = render_readme(manifest)

    assert "[octocat/repo-7](https://github.com/octocat/repo-7)" in markdown
    assert "javascript:" not in markdown
    assert "next line" not in markdown


def test_output_path_symlinks_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeOutputPathError):
        publish_catalog(_manifest(), output)
    assert list(outside.iterdir()) == []
