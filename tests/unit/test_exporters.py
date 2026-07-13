"""Tests for validated catalog construction and deterministic publication."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
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
    CatalogFormat,
    UnsafeOutputPathError,
    publish_catalog,
    render_catalog_json,
    render_catalog_yaml,
    render_module_page,
    render_readme,
)
from github_module_catalog.models import (
    CapabilityAssertion,
    CatalogManifest,
    CatalogSelectionCriteria,
    RepositoryIdentity,
    RepositoryObservation,
    ReuseStatus,
)
from github_module_catalog.source import (
    RateLimitFacts,
    RepositoryInventoryIdentity,
    RepositoryPage,
)
from github_module_catalog.state import StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import Taxonomy, classify_repository, load_taxonomy

NOW = datetime(2026, 7, 13, tzinfo=UTC)
TAXONOMY_PATH = (
    Path(__file__).parents[2] / "src" / "github_module_catalog" / "data" / "taxonomy.yaml"
)


def _observation(
    repository_id: int,
    *,
    topic: str = "cli",
    license_spdx: str | None = "MIT",
    description: str = "A useful CLI",
    stargazers_count: int | None = None,
    pushed_at: datetime | None = None,
    archived: bool | None = None,
    fork: bool | None = None,
    private: bool | None = None,
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
        pushed_at=pushed_at,
        stargazers_count=stargazers_count,
        observed_at=NOW,
        archived=archived,
        fork=fork,
        private=private,
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


def _ranked_context(
    repository_ranks: tuple[tuple[int, int], ...],
) -> CatalogBuildContext:
    return CatalogBuildContext(
        source="github-search-repositories",
        discovered_count=len(repository_ranks),
        selection=CatalogSelectionCriteria(
            min_stars=100,
            pushed_since=datetime(2025, 7, 13, tzinfo=UTC),
            result_limit=1_000,
        ),
        api_total_count=2_500,
        pages_fetched=10,
        result_limit=1_000,
        repository_ranks=repository_ranks,
        coverage_note="Top ranked GitHub Search window; not all public repositories.",
    )


def _ranked_manifest() -> CatalogManifest:
    return build_catalog(
        (
            _observation(
                9,
                topic="auth",
                stargazers_count=200,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
            _observation(
                8,
                stargazers_count=300,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
            _observation(
                2,
                stargazers_count=200,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
        ),
        taxonomy=load_taxonomy(TAXONOMY_PATH),
        context=_ranked_context(((8, 1), (2, 2), (9, 3))),
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


def test_catalog_build_publishes_sorted_capability_hierarchy_contract() -> None:
    manifest = _manifest()

    assert manifest.schema_version == "1.1.0"
    assert [definition.id for definition in manifest.capability_definitions] == sorted(
        node.id for node in load_taxonomy(TAXONOMY_PATH).axes["capability"]
    )
    auth = next(
        definition for definition in manifest.capability_definitions if definition.id == "auth"
    )
    assert auth.label == "Authentication and authorization"
    assert auth.parents == ("security",)


def _commit_page(
    state: StateStore,
    raw_store: RawObjectStore,
    source: str,
    repository_id: int,
) -> str:
    name = f"repo-{repository_id}"
    raw_bytes = json.dumps([{"id": repository_id}], separators=(",", ":")).encode()
    raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    page = RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        etag=None,
        next_url=f"https://api.github.com/repositories?since={repository_id}",
        next_cursor=repository_id,
        rate_limit=RateLimitFacts(),
        identities=(
            RepositoryInventoryIdentity(
                repository_id=repository_id,
                name=name,
                full_name=f"octocat/{name}",
                owner_login="octocat",
                owner_id=1,
                html_url=f"https://github.com/octocat/{name}",
            ),
        ),
    )
    run = state.create_crawl_run(source, started_at=NOW)
    raw_store.write(raw_bytes, expected_sha256=raw_sha256)
    state.commit_discovery_page(
        run.id,
        cursor_before=run.discovery_cursor,
        page=page,
        committed_at=NOW,
    )
    return raw_sha256


def test_state_catalog_build_derives_source_scoped_manifest_and_cannot_be_forged(
    tmp_path: Path,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    raw_store = RawObjectStore(tmp_path)
    state = StateStore(tmp_path / "state.sqlite3", raw_store)
    first_hash = _commit_page(state, raw_store, "github", 7)
    second_hash = _commit_page(state, raw_store, "github", 8)
    _commit_page(state, raw_store, "archive", 9)
    first_observation = _observation(7)
    state.record_repository_observation(first_observation)
    state.record_repository_observation(_observation(9))
    state.append_work_item_event(7, "enrichment", "retry", occurred_at=NOW)
    state.append_work_item_event(8, "enrichment", "dead_letter", occurred_at=NOW)

    snapshot = state.catalog_snapshot("github")
    manifest = build_catalog_from_state(state, taxonomy=taxonomy, source="github")

    assert snapshot.source == "github"
    assert (snapshot.cursor_start, snapshot.cursor_end) == (0, 8)
    assert snapshot.discovered_count == 2
    assert snapshot.validated_observation_count == 1
    assert (snapshot.pending_count, snapshot.retry_count, snapshot.dead_letter_count) == (0, 1, 1)
    assert snapshot.raw_page_hashes == tuple(sorted((first_hash, second_hash)))
    assert snapshot.observations == (first_observation,)
    with pytest.raises(FrozenInstanceError):
        snapshot.cursor_end = 999  # type: ignore[misc]

    assert manifest.source == snapshot.source
    assert (manifest.cursor_start, manifest.cursor_end) == (0, 8)
    assert manifest.discovered_count == 2
    assert manifest.validated_observation_count == 1
    assert (manifest.pending_count, manifest.retry_count, manifest.dead_letter_count) == (0, 1, 1)
    assert manifest.raw_page_hashes == snapshot.raw_page_hashes
    assert manifest.source_hashes == (first_observation.stable_hash(),)
    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [7]
    with pytest.raises(TypeError, match="context"):
        build_catalog_from_state(  # type: ignore[call-arg]
            state,
            taxonomy=taxonomy,
            source="github",
            context=_context(),
        )

    second_observation = _observation(8, topic="auth", description="Auth service")
    state.record_repository_observation(second_observation)
    state.append_work_item_event(7, "enrichment", "queued", occurred_at=NOW)
    updated = build_catalog_from_state(state, taxonomy=taxonomy, source="github")

    assert updated.validated_observation_count == 2
    assert (updated.pending_count, updated.retry_count, updated.dead_letter_count) == (1, 0, 1)
    assert [entry.repository.identity.repository_id for entry in updated.entries] == [7, 8]


def test_explicit_catalog_builder_rejects_unvalidated_observations() -> None:
    with pytest.raises(TypeError, match="RepositoryObservation"):
        build_catalog(
            ({"id": 7},),  # type: ignore[arg-type]
            taxonomy=load_taxonomy(TAXONOMY_PATH),
            context=_context(),
        )


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


def test_ranked_catalog_builder_uses_immutable_repository_rank_mapping() -> None:
    context = _ranked_context(((8, 1), (2, 2), (9, 3)))

    manifest = _ranked_manifest()

    assert context.repository_ranks == ((8, 1), (2, 2), (9, 3))
    with pytest.raises(FrozenInstanceError):
        context.repository_ranks = ((8, 1),)  # type: ignore[misc]
    assert [entry.rank for entry in manifest.entries] == [1, 2, 3]
    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [8, 2, 9]
    assert manifest.selection == context.selection
    assert manifest.api_total_count == 2_500
    assert manifest.pages_fetched == 10
    assert manifest.result_limit == 1_000


def test_rank_mapping_tuple_order_does_not_change_mapping_semantics() -> None:
    context = _ranked_context(((9, 3), (8, 1), (2, 2)))

    manifest = build_catalog(
        (
            _observation(
                9,
                stargazers_count=200,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
            _observation(
                8,
                stargazers_count=300,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
            _observation(
                2,
                stargazers_count=200,
                pushed_at=NOW,
                archived=False,
                fork=False,
                private=False,
            ),
        ),
        taxonomy=load_taxonomy(TAXONOMY_PATH),
        context=context,
    )

    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [8, 2, 9]


def test_ranked_catalog_builder_rejects_mutable_or_incomplete_rank_mapping() -> None:
    class MutableSelection:
        result_limit = 1_000

    with pytest.raises(TypeError, match="CatalogSelectionCriteria"):
        CatalogBuildContext(
            selection=MutableSelection(),  # type: ignore[arg-type]
            api_total_count=1,
            pages_fetched=1,
            result_limit=1_000,
            repository_ranks=((7, 1),),
        )

    with pytest.raises(TypeError, match="immutable tuple"):
        CatalogBuildContext(
            selection=CatalogSelectionCriteria(
                min_stars=100,
                pushed_since=datetime(2025, 7, 13, tzinfo=UTC),
                result_limit=1_000,
            ),
            api_total_count=1,
            pages_fetched=1,
            result_limit=1_000,
            repository_ranks={7: 1},  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="rank mapping"):
        build_catalog(
            (
                _observation(
                    7,
                    stargazers_count=100,
                    pushed_at=NOW,
                    archived=False,
                    fork=False,
                    private=False,
                ),
            ),
            taxonomy=load_taxonomy(TAXONOMY_PATH),
            context=_ranked_context(()),
        )


def test_ranked_catalog_builder_rejects_rank_mapping_that_forges_star_order() -> None:
    observations = (
        _observation(
            9,
            stargazers_count=100,
            pushed_at=NOW,
            archived=False,
            fork=False,
            private=False,
        ),
        _observation(
            2,
            stargazers_count=200,
            pushed_at=NOW,
            archived=False,
            fork=False,
            private=False,
        ),
    )

    with pytest.raises(ValueError, match="stars descending"):
        build_catalog(
            observations,
            taxonomy=load_taxonomy(TAXONOMY_PATH),
            context=_ranked_context(((9, 1), (2, 2))),
        )


def test_json_yaml_and_markdown_have_equivalent_sorted_catalog_entries() -> None:
    manifest = _manifest()
    json_document = json.loads(render_catalog_json(manifest))
    yaml_document = yaml.safe_load(render_catalog_yaml(manifest))
    markdown = render_readme(manifest)

    assert json_document == yaml_document
    assert [
        entry["repository"]["identity"]["repository_id"] for entry in json_document["entries"]
    ] == [2, 9]
    assert [item["capability_id"] for item in json_document["entries"][1]["assertions"]] == [
        "auth",
        "security",
    ]
    assert markdown.index("octocat/repo-2") < markdown.index("octocat/repo-9")
    assert "`auth`" in markdown and "`cli`" in markdown
    assert "| Repository ID | Repository | Capabilities | License | Reuse status |" in markdown
    assert "| Rank | Stars | Last push |" not in markdown
    source_line = next(line for line in markdown.splitlines() if line.startswith("Source:"))
    assert source_line == "Source: `github-public-repositories`; cursor: `0` through `9`."
    assert "| Repository ID | Repository | Confidence | License | Reuse status |" in (
        render_module_page(manifest, "auth")
    )


def test_source_with_backticks_stays_inside_a_safe_markdown_code_span() -> None:
    manifest = build_catalog(
        (_observation(7),),
        taxonomy=load_taxonomy(TAXONOMY_PATH),
        context=replace(_context(), source="github` <img src=x>"),
    )

    markdown = render_readme(manifest)

    assert "Source: `` github` <img src=x> ``; cursor:" in markdown
    assert "Source: `github\\` <img src=x>`;" not in markdown


def test_ranked_json_yaml_and_markdown_share_stars_desc_id_tiebreak_order() -> None:
    manifest = _ranked_manifest()
    json_document = json.loads(render_catalog_json(manifest))
    yaml_document = yaml.safe_load(render_catalog_yaml(manifest))
    markdown = render_readme(manifest)

    assert json_document == yaml_document
    assert [entry["rank"] for entry in json_document["entries"]] == [1, 2, 3]
    assert [
        entry["repository"]["identity"]["repository_id"] for entry in json_document["entries"]
    ] == [8, 2, 9]
    assert markdown.index("octocat/repo-8") < markdown.index("octocat/repo-2")
    assert markdown.index("octocat/repo-2") < markdown.index("octocat/repo-9")


def test_ranked_markdown_explains_selection_and_search_window_coverage() -> None:
    manifest = _ranked_manifest()

    readme = render_readme(manifest)
    module_page = render_module_page(manifest, "auth")

    assert (
        "| Rank | Stars | Last push | Repository | Capabilities | License | Reuse status |"
        in readme
    )
    assert "Minimum stars: `100`" in readme
    assert "Pushed since: `2025-07-13T00:00:00Z`" in readme
    assert "Archived: `false`; forks: `false`; visibility: `public`" in readme
    assert "Order: `stars desc`" in readme
    assert "Top `3` of `2500` matching repositories" in readme
    assert "result limit: `1000`" in readme
    assert "pages fetched: `10`" in readme
    assert "cursor:" not in readme
    for selection_fact in (
        "## Selection",
        "Minimum stars: `100`",
        "Pushed since: `2025-07-13T00:00:00Z`",
        "Archived: `false`; forks: `false`; visibility: `public`",
        "Order: `stars desc`",
        "Top `3` of `2500` matching repositories",
        "result limit: `1000`",
        "pages fetched: `10`",
    ):
        assert selection_fact in module_page
    assert module_page.index("## Selection") < module_page.index("| Rank | Stars |")
    assert (
        "| Rank | Stars | Last push | Repository | Confidence | License | Reuse status |"
        in module_page
    )
    assert "| 3 | 200 | 2026-07-13T00:00:00Z |" in module_page


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
        Path("modules/security.md"),
    }
    assert "generated_at" not in json.loads(first[Path("manifest.json")])
    timestamped = _manifest(generated_at=NOW)
    assert json.loads(render_catalog_json(timestamped))["generated_at"] == "2026-07-13T00:00:00Z"


def test_publication_emits_only_an_immutable_selected_format_set(tmp_path: Path) -> None:
    output = tmp_path / "output"

    artifacts = publish_catalog(_manifest(), output, formats=frozenset({CatalogFormat.JSON}))

    assert {path.relative_to(output).as_posix() for path in artifacts} == {
        "catalog.json",
        "manifest.json",
    }
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["artifacts"]) == {"catalog.json"}
    with pytest.raises(TypeError, match="frozenset"):
        publish_catalog(
            _manifest(),
            tmp_path / "mutable",
            formats={CatalogFormat.JSON},  # type: ignore[arg-type]
        )


def test_manifest_reports_coverage_counts_versions_and_source_hashes() -> None:
    manifest = _manifest()

    assert manifest.source == "github-public-repositories"
    assert (manifest.cursor_start, manifest.cursor_end) == (0, 9)
    assert (manifest.discovered_count, manifest.validated_observation_count) == (4, 2)
    assert (manifest.pending_count, manifest.retry_count, manifest.dead_letter_count) == (2, 1, 1)
    assert manifest.schema_version == "1.1.0"
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


def test_coverage_note_is_escaped_as_untrusted_single_line_markdown() -> None:
    malicious = (
        "> quote\n- list\nCoverage\n# forged\n![x](javascript:alert(1)) <img src=x> **bold**"
    )
    manifest = build_catalog(
        (_observation(7),),
        taxonomy=load_taxonomy(TAXONOMY_PATH),
        context=replace(_context(), coverage_note=malicious),
    )

    markdown = render_readme(manifest)

    assert "\n# forged" not in markdown
    assert "![x]" not in markdown
    assert "<img" not in markdown
    assert "&gt; quote \\- list" in markdown
    assert "Coverage \\# forged" in markdown
    assert "\\!\\[x\\]\\(javascript:alert\\(1\\)\\)" in markdown
    assert "&lt;img src=x&gt;" in markdown
    assert "\\*\\*bold\\*\\*" in markdown


def test_output_path_symlinks_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "output"
    output.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeOutputPathError):
        publish_catalog(_manifest(), output)
    assert list(outside.iterdir()) == []


def test_publication_rejects_output_inode_swap_without_touching_external_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    publish_catalog(_manifest(), output)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    real_rename = os.rename
    swapped = False

    def swapping_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and source == "output" and ".backup-" in target:
            swapped = True
            real_rename(
                source,
                "stolen-output",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            os.symlink(outside, source, dir_fd=src_dir_fd, target_is_directory=True)
        real_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", swapping_rename)

    with pytest.raises(UnsafeOutputPathError, match="changed during publication"):
        publish_catalog(_manifest(), output)

    assert marker.read_text() == "keep"


def test_publication_rejects_staging_inode_swap_without_following_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    real_rename = os.rename
    swapped = False

    def swapping_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if not swapped and ".stage-" in source and target == "output":
            swapped = True
            real_rename(
                source,
                "stolen-stage",
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            os.symlink(outside, source, dir_fd=src_dir_fd, target_is_directory=True)
        real_rename(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", swapping_rename)

    with pytest.raises(UnsafeOutputPathError, match="changed during publication"):
        publish_catalog(_manifest(), output)

    assert marker.read_text() == "keep"
