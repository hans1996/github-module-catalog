"""Durability tests for raw objects and resumable SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import HttpUrl

from github_module_catalog.models import CatalogManifest, RepositoryIdentity, RepositoryObservation
from github_module_catalog.source import (
    RateLimitFacts,
    RepositoryInventoryIdentity,
    RepositoryPage,
)
from github_module_catalog.state import SensitiveStateError, StateConflictError, StateStore
from github_module_catalog.storage import (
    DigestMismatchError,
    InvalidDigestError,
    ObjectCollisionError,
    ObjectSizeLimitError,
    RawObjectStore,
)

NOW = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)


def _page(
    repository_id: int = 7,
    *,
    owner: str = "octocat",
    name: str = "catalog",
    next_cursor: int | None = None,
    next_url: str | None = None,
) -> RepositoryPage:
    raw_bytes = json.dumps(
        [{"id": repository_id, "name": name, "owner": {"login": owner}}],
        separators=(",", ":"),
    ).encode()
    identity = RepositoryInventoryIdentity(
        repository_id=repository_id,
        name=name,
        full_name=f"{owner}/{name}",
        owner_login=owner,
        owner_id=repository_id + 100,
        html_url=f"https://github.com/{owner}/{name}",
    )
    return RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        etag='"page-etag"',
        next_url=next_url,
        next_cursor=repository_id if next_cursor is None else next_cursor,
        rate_limit=RateLimitFacts(limit=60, remaining=59, reset_epoch=1234, resource="core"),
        identities=(identity,),
    )


def _page_for_ids(*repository_ids: int) -> RepositoryPage:
    raw_bytes = json.dumps(
        [{"id": repository_id} for repository_id in repository_ids],
        separators=(",", ":"),
    ).encode()
    return RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        etag=None,
        next_url=None,
        next_cursor=max(repository_ids),
        rate_limit=RateLimitFacts(),
        identities=tuple(
            RepositoryInventoryIdentity(
                repository_id=repository_id,
                name=f"repo-{repository_id}",
                full_name=f"octocat/repo-{repository_id}",
                owner_login="octocat",
                owner_id=100 + repository_id,
                html_url=f"https://github.com/octocat/repo-{repository_id}",
            )
            for repository_id in repository_ids
        ),
    )


def _stores(tmp_path: Path) -> tuple[RawObjectStore, StateStore]:
    raw_store = RawObjectStore(tmp_path)
    return raw_store, StateStore(tmp_path / "data" / "state.sqlite3", raw_store)


def test_state_database_symlink_is_rejected_without_modifying_target(tmp_path: Path) -> None:
    external = tmp_path / "external.sqlite3"
    with sqlite3.connect(external) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    original = external.read_bytes()
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "state.sqlite3").symlink_to(external)

    with pytest.raises(sqlite3.OperationalError):
        StateStore(workspace / "data" / "state.sqlite3", RawObjectStore(workspace))

    assert external.read_bytes() == original


def test_state_parent_swap_cannot_redirect_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    data = workspace / "data"
    data.mkdir(parents=True)
    moved_data = workspace / "trusted-data"
    external = tmp_path / "external"
    external.mkdir()
    real_connect = sqlite3.connect
    swapped = False

    def swapping_connect(
        database: str, *, isolation_level: None = None, uri: bool = False
    ) -> sqlite3.Connection:
        nonlocal swapped
        del isolation_level
        if not swapped:
            swapped = True
            data.rename(moved_data)
            data.symlink_to(external, target_is_directory=True)
        return real_connect(database, isolation_level=None, uri=uri)

    monkeypatch.setattr(sqlite3, "connect", swapping_connect)

    state = StateStore(data / "state.sqlite3", RawObjectStore(workspace))
    state.close()

    assert not (external / "state.sqlite3").exists()
    assert (moved_data / "state.sqlite3").is_file()


def _mark_mapping_migration_pending(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS schema_migrations")


def test_raw_write_is_content_addressed_fsynced_and_atomically_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"public":true}'
    digest = hashlib.sha256(payload).hexdigest()
    fsynced: list[int] = []
    links: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]] = []
    real_fsync = os.fsync
    real_link = os.link

    def observed_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    def observed_link(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        links.append((source, target))
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "fsync", observed_fsync)
    monkeypatch.setattr(os, "link", observed_link)

    stored = RawObjectStore(tmp_path).write(payload, expected_sha256=digest)

    expected_path = tmp_path / "data" / "raw" / "sha256" / digest[:2] / f"{digest}.json"
    assert stored.path == expected_path
    assert stored.sha256 == digest
    assert stored.size_bytes == len(payload)
    assert expected_path.read_bytes() == payload
    assert fsynced
    assert len(links) == 1
    assert Path(links[0][1]).name == expected_path.name


def test_raw_write_never_clobbers_a_symlink_that_wins_the_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawObjectStore(tmp_path)
    payload = b'{"safe":true}'
    digest = hashlib.sha256(payload).hexdigest()
    target = store.path_for(digest)
    target.parent.mkdir(parents=True)
    attacker = tmp_path / "attacker.json"
    attacker.write_bytes(b"attacker-owned")
    real_link = os.link

    def racing_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        target.symlink_to(attacker)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", racing_link)

    with pytest.raises(ObjectCollisionError):
        store.write(payload)
    assert target.is_symlink()
    assert attacker.read_bytes() == b"attacker-owned"


def test_raw_write_is_idempotent_and_does_not_replace_an_existing_object(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    payload = b"[]"

    first = store.write(payload)
    first_stat = first.path.stat()
    second = store.write(payload)

    assert second == first
    assert second.path.stat().st_mtime_ns == first_stat.st_mtime_ns


def test_raw_object_store_enforces_write_and_forged_inode_size_limits(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path, max_object_bytes=8)

    with pytest.raises(ObjectSizeLimitError, match="size limit"):
        store.write(b"123456789")

    stored = store.write(b"x")
    stored.path.write_bytes(b"123456789")
    with pytest.raises(ObjectSizeLimitError, match="size limit"):
        store.read(stored.sha256)


def test_raw_write_rejects_digest_mismatch_collision_and_path_traversal(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    payload = b"[]"
    digest = hashlib.sha256(payload).hexdigest()

    with pytest.raises(DigestMismatchError):
        store.write(payload, expected_sha256="0" * 64)
    with pytest.raises(InvalidDigestError):
        store.path_for("../state.sqlite3")

    target = store.path_for(digest)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    with pytest.raises(ObjectCollisionError):
        store.write(payload, expected_sha256=digest)
    assert target.read_bytes() == b"corrupt"


def test_raw_read_returns_the_bytes_from_the_same_verified_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawObjectStore(tmp_path)
    payload = b'{"stable":true}'
    stored = store.write(payload)
    real_read_bytes = Path.read_bytes
    reads = 0

    def replacing_read(path: Path) -> bytes:
        nonlocal reads
        observed = real_read_bytes(path)
        if path == stored.path:
            reads += 1
            if reads == 1:
                stored.path.write_bytes(b"replacement")
        return observed

    monkeypatch.setattr(Path, "read_bytes", replacing_read)

    assert store.read(stored.sha256) == payload
    assert reads <= 1


def test_cursor_advances_only_after_raw_object_and_page_transaction_are_durable(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    page = _page()

    with pytest.raises(FileNotFoundError):
        state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    assert state.get_discovery_cursor(run.id) == 0
    assert state.list_discovery_pages(run.id) == ()

    raw_store.write(page.raw_bytes, expected_sha256=page.raw_sha256)
    committed = state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)

    assert committed.cursor_after == 7
    assert committed.raw_sha256 == page.raw_sha256
    assert state.get_discovery_cursor(run.id) == 7
    assert len(state.list_repository_identities()) == 1
    assert [(event.repository_id, event.event) for event in state.list_work_item_events()] == [
        (7, "queued")
    ]


def test_duplicate_numeric_ids_are_idempotent_and_renames_require_an_observation(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    first_page = _page()
    renamed_page = _page(owner="new-owner", name="renamed", next_cursor=7)
    for page in (first_page, renamed_page):
        raw_store.write(page.raw_bytes, expected_sha256=page.raw_sha256)
    state.commit_discovery_page(run.id, cursor_before=0, page=first_page, committed_at=NOW)
    state.commit_discovery_page(run.id, cursor_before=7, page=renamed_page, committed_at=NOW)

    identities = state.list_repository_identities()
    assert len(identities) == 1
    assert (identities[0].owner_login, identities[0].name) == ("octocat", "catalog")
    assert len(state.list_work_item_events()) == 1

    observation = RepositoryObservation(
        identity=RepositoryIdentity(repository_id=7),
        owner="new-owner",
        name="renamed",
        full_name="new-owner/renamed",
        html_url=HttpUrl("https://github.com/new-owner/renamed"),
        description=None,
        topics=(),
        primary_language="Python",
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
        license_spdx="MIT",
        license_name="MIT License",
    )
    state.record_repository_observation(observation)

    updated = state.list_repository_identities()
    assert (updated[0].owner_login, updated[0].name) == ("new-owner", "renamed")
    assert state.list_repository_observations(7)[0].observation_hash == observation.stable_hash()


def test_injected_failure_rolls_back_page_identities_events_and_cursor(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes, expected_sha256=page.raw_sha256)

    def fail_before_commit() -> None:
        raise RuntimeError("injected transaction failure")

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        state.commit_discovery_page(
            run.id,
            cursor_before=0,
            page=page,
            committed_at=NOW,
            before_commit=fail_before_commit,
        )

    assert state.get_discovery_cursor(run.id) == 0
    assert state.list_discovery_pages(run.id) == ()
    assert state.list_repository_identities() == ()
    assert state.list_work_item_events() == ()


@pytest.mark.parametrize("next_cursor", [999, 6])
def test_state_rejects_unverified_page_cursor_before_advancing_state(
    tmp_path: Path, next_cursor: int
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page(
        7,
        next_cursor=next_cursor,
        next_url=f"https://api.github.com/repositories?since={next_cursor}",
    )
    raw_store.write(page.raw_bytes)

    with pytest.raises(ValueError, match="cursor"):
        state.commit_discovery_page(
            run.id,
            cursor_before=0,
            page=page,
            committed_at=NOW,
        )

    assert state.get_discovery_cursor(run.id) == 0
    assert state.list_discovery_pages(run.id) == ()
    assert state.list_repository_identities() == ()


def test_stage_checkpoints_are_independent_and_return_frozen_records(tmp_path: Path) -> None:
    _, state = _stores(tmp_path)

    enrichment = state.set_stage_checkpoint("enrichment", "repo:7", updated_at=NOW)
    classification = state.set_stage_checkpoint("classification", "repo:3", updated_at=NOW)

    assert state.get_stage_checkpoint("enrichment") == enrichment
    assert state.get_stage_checkpoint("classification") == classification
    with pytest.raises(FrozenInstanceError):
        enrichment.value = "repo:8"  # type: ignore[misc]


def test_new_crawl_run_resumes_only_the_same_source_cursor(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    github_run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(github_run.id, cursor_before=0, page=page, committed_at=NOW)

    archive_run = state.create_crawl_run("archive", started_at=NOW)
    resumed_github_run = state.create_crawl_run("github", started_at=NOW)

    assert archive_run.discovery_cursor == 0
    assert resumed_github_run.discovery_cursor == 7


def test_work_queue_replay_is_idempotent_while_event_history_remains_append_only(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)

    first = state.queue_work_item(7, "classification", "revision-1", "classifier-v1", queued_at=NOW)
    replay = state.queue_work_item(
        7, "classification", "revision-1", "classifier-v1", queued_at=NOW
    )

    assert first is True
    assert replay is False
    assert len(state.list_work_items(stage="classification")) == 1
    assert tuple(
        event.event
        for event in state.list_work_item_events(repository_id=7, stage="classification")
    ) == ("queued",)


def test_work_item_events_preserve_append_only_history(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)

    state.append_work_item_event(7, "classification", "retry", occurred_at=NOW)
    state.append_work_item_event(7, "classification", "queued", occurred_at=NOW)

    history = state.list_work_item_events(repository_id=7, stage="classification")
    assert tuple(event.event for event in history) == ("retry", "queued")
    with sqlite3.connect(state.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE work_item_events SET event = ?", ("published",))


def test_state_rejects_credential_material_and_has_all_required_schema_tables(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    page = _page(next_url="https://api.github.com/repositories?since=7&access_token=secret")
    raw_store.write(page.raw_bytes)

    with pytest.raises(SensitiveStateError):
        state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    with pytest.raises(SensitiveStateError):
        state.append_work_item_event(
            7,
            "enrichment",
            "retry",
            occurred_at=NOW,
            details={"Authorization": "Bearer secret-token"},
        )
    valid_page = _page()
    state.commit_discovery_page(run.id, cursor_before=0, page=valid_page, committed_at=NOW)
    with pytest.raises(SensitiveStateError):
        state.append_work_item_event(
            7,
            "enrichment",
            "retry",
            occurred_at=NOW,
            details={"error": "Authorization: Bearer secret-token"},
        )

    with sqlite3.connect(state.path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
            )
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    assert {
        "crawl_runs",
        "discovery_pages",
        "discovery_page_repositories",
        "repository_identities",
        "repository_observations",
        "schema_migrations",
        "work_item_events",
        "work_items",
        "stage_checkpoints",
        "catalog_publications",
    } <= tables
    assert foreign_keys == (0,)  # A new connection must opt in; StateStore itself enables it.
    assert b"secret-token" not in state.path.read_bytes()
    assert state.foreign_keys_enabled is True
    assert state.journal_mode in {"wal", "memory"}


def test_catalog_publication_rejects_same_semantics_with_different_artifacts(
    tmp_path: Path,
) -> None:
    _raw_store, state = _stores(tmp_path)
    manifest = CatalogManifest(
        schema_version="1.0.0",
        taxonomy_version="1.0.0",
        classifier_version="rules-v1",
        generated_at=NOW,
        source="github",
    )
    first = state.record_catalog_publication(
        manifest,
        artifact_manifest_sha256="a" * 64,
        published_at=NOW,
    )

    repeated = state.record_catalog_publication(
        manifest,
        artifact_manifest_sha256="a" * 64,
        published_at=NOW,
    )
    assert repeated == first
    with pytest.raises(StateConflictError, match="different artifacts"):
        state.record_catalog_publication(
            manifest,
            artifact_manifest_sha256="b" * 64,
            published_at=NOW,
        )


def test_legacy_database_backfills_source_links_from_verified_raw_pages(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    state.close()
    with sqlite3.connect(tmp_path / "data" / "state.sqlite3") as connection:
        connection.execute("DROP TABLE discovery_page_repositories")
    _mark_mapping_migration_pending(tmp_path / "data" / "state.sqlite3")

    reopened = StateStore(tmp_path / "data" / "state.sqlite3", raw_store)
    snapshot = reopened.catalog_snapshot("github")

    assert snapshot.discovered_count == 1
    assert snapshot.raw_page_hashes == (page.raw_sha256,)
    assert snapshot.pending_count == 1


@pytest.mark.parametrize(
    ("damage", "error_type"),
    [("missing", FileNotFoundError), ("corrupt", ObjectCollisionError)],
)
def test_legacy_backfill_fails_closed_when_raw_page_is_missing_or_corrupt(
    tmp_path: Path,
    damage: str,
    error_type: type[Exception],
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    stored = raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    state.close()
    with sqlite3.connect(tmp_path / "data" / "state.sqlite3") as connection:
        connection.execute("DROP TABLE discovery_page_repositories")
    _mark_mapping_migration_pending(tmp_path / "data" / "state.sqlite3")
    if damage == "missing":
        stored.path.unlink()
    else:
        stored.path.write_bytes(b"corrupt")

    with pytest.raises(error_type):
        StateStore(tmp_path / "data" / "state.sqlite3", raw_store)


def test_mapping_migration_rejects_raw_repository_without_identity(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    committed = state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    state.close()
    legacy_raw = json.dumps([{"id": 7}, {"id": 8}], separators=(",", ":")).encode()
    legacy_object = raw_store.write(legacy_raw)
    with sqlite3.connect(tmp_path / "data" / "state.sqlite3") as connection:
        connection.execute(
            "UPDATE discovery_pages SET raw_sha256 = ?, raw_size_bytes = ? WHERE id = ?",
            (legacy_object.sha256, len(legacy_raw), committed.id),
        )
    _mark_mapping_migration_pending(tmp_path / "data" / "state.sqlite3")

    with pytest.raises(StateConflictError, match="missing repository identities"):
        StateStore(tmp_path / "data" / "state.sqlite3", raw_store)


def test_mapping_migration_repairs_partially_linked_page_exactly(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page_for_ids(7, 8)
    raw_store.write(page.raw_bytes)
    committed = state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    state.close()
    with sqlite3.connect(tmp_path / "data" / "state.sqlite3") as connection:
        connection.execute(
            "DELETE FROM discovery_page_repositories WHERE page_id = ? AND repository_id = ?",
            (committed.id, 8),
        )
    _mark_mapping_migration_pending(tmp_path / "data" / "state.sqlite3")

    reopened = StateStore(tmp_path / "data" / "state.sqlite3", raw_store)

    assert reopened.catalog_snapshot("github").discovered_count == 2
    with sqlite3.connect(reopened.path) as connection:
        linked = connection.execute(
            "SELECT repository_id FROM discovery_page_repositories WHERE page_id = ? "
            "ORDER BY repository_id",
            (committed.id,),
        ).fetchall()
    assert linked == [(7,), (8,)]


def test_existing_page_replay_rejects_extra_repository_link(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    first_run = state.create_crawl_run("github", started_at=NOW)
    first_page = _page(7)
    raw_store.write(first_page.raw_bytes)
    first = state.commit_discovery_page(
        first_run.id, cursor_before=0, page=first_page, committed_at=NOW
    )
    second_run = state.create_crawl_run("github", started_at=NOW)
    second_page = _page(8, next_cursor=8)
    raw_store.write(second_page.raw_bytes)
    state.commit_discovery_page(
        second_run.id,
        cursor_before=second_run.discovery_cursor,
        page=second_page,
        committed_at=NOW,
    )
    with sqlite3.connect(state.path) as connection:
        connection.execute(
            "INSERT INTO discovery_page_repositories(page_id, repository_id) VALUES (?, ?)",
            (first.id, 8),
        )

    with pytest.raises(StateConflictError, match="extra repository links"):
        state.commit_discovery_page(
            first_run.id,
            cursor_before=0,
            page=first_page,
            committed_at=NOW,
        )


def test_existing_page_replay_repairs_missing_repository_links(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    with sqlite3.connect(state.path) as connection:
        connection.execute("DELETE FROM discovery_page_repositories")

    replay = state.commit_discovery_page(
        run.id,
        cursor_before=0,
        page=page,
        committed_at=NOW,
    )

    assert replay.newly_committed is False
    assert state.catalog_snapshot("github").discovered_count == 1


def test_delayed_observation_does_not_regress_identity_or_latest_catalog_fact(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    newer = RepositoryObservation(
        identity=RepositoryIdentity(repository_id=7),
        owner="new-owner",
        name="new-name",
        full_name="new-owner/new-name",
        html_url=HttpUrl("https://github.com/new-owner/new-name"),
        topics=(),
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW + timedelta(days=2),
        license_spdx="MIT",
    )
    older = newer.model_copy(
        update={
            "owner": "old-owner",
            "name": "old-name",
            "full_name": "old-owner/old-name",
            "html_url": HttpUrl("https://github.com/old-owner/old-name"),
            "observed_at": NOW + timedelta(days=1),
        }
    )

    state.record_repository_observation(newer)
    state.record_repository_observation(older)

    identity = state.list_repository_identities()[0]
    assert (identity.owner_login, identity.name) == ("new-owner", "new-name")
    assert state.list_latest_repository_observations() == (newer,)
    assert state.catalog_snapshot("github").observations == (newer,)


def test_latest_observation_uses_insertion_id_to_break_timestamp_ties(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    first = RepositoryObservation(
        identity=RepositoryIdentity(repository_id=7),
        owner="first-owner",
        name="first-name",
        full_name="first-owner/first-name",
        html_url=HttpUrl("https://github.com/first-owner/first-name"),
        topics=(),
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
    )
    second = first.model_copy(
        update={
            "owner": "second-owner",
            "name": "second-name",
            "full_name": "second-owner/second-name",
            "html_url": HttpUrl("https://github.com/second-owner/second-name"),
        }
    )

    state.record_repository_observation(first)
    state.record_repository_observation(second)

    assert state.list_latest_repository_observations() == (second,)
    assert state.catalog_snapshot("github").observations == (second,)


@pytest.mark.parametrize(
    "credential",
    [
        "access_token=secret-value",
        "Access Token : secret-value",
        "token: secret-value",
        "X-Api-Key: secret-value",
        "x_api_key = secret-value",
        "api_key=secret-value",
        "github_token=secret-value",
        "client_secret=secret-value",
        "password=secret-value",
        "Authorization: Basic secret-value",
        "Bearer secret-value",
        "gh" + "p_abcdefghijklmnopqrstuvwxyz123456",
        "github_" + "pat_abcdefghijklmnopqrstuvwxyz123456",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "sk-" + "abcdefghijklmnopqrstuvwxyz123456",
        "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature123",
        "-----BEGIN " + "PRIVATE KEY-----",
        "glpat-" + "abcdefghijklmnopqrstuvwxyz",
        "xoxb-" + "1234567890-abcdefghijklmnop",
        "sk_live_" + "abcdefghijklmnop1234",
        "https://user:password@example.com/private",
    ],
)
def test_state_rejects_embedded_credential_values_recursively(
    tmp_path: Path, credential: str
) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)

    with pytest.raises(SensitiveStateError):
        state.append_work_item_event(
            7,
            "enrichment",
            "retry",
            occurred_at=NOW,
            details={"context": [{"error": f"request failed: {credential}"}]},
        )

    assert credential.encode() not in state.path.read_bytes()


def test_state_allows_benign_prose_that_mentions_token_without_a_value(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    run = state.create_crawl_run("github", started_at=NOW)
    page = _page()
    raw_store.write(page.raw_bytes)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)

    event = state.append_work_item_event(
        7,
        "enrichment",
        "retry",
        occurred_at=NOW,
        details={"message": "Token rotation is documented without storing a credential value."},
    )

    assert event.event == "retry"
