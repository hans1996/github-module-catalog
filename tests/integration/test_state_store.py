"""Durability tests for raw objects and resumable SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import HttpUrl

from github_module_catalog.models import RepositoryIdentity, RepositoryObservation
from github_module_catalog.source import (
    RateLimitFacts,
    RepositoryInventoryIdentity,
    RepositoryPage,
)
from github_module_catalog.state import SensitiveStateError, StateStore
from github_module_catalog.storage import (
    DigestMismatchError,
    InvalidDigestError,
    ObjectCollisionError,
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


def _stores(tmp_path: Path) -> tuple[RawObjectStore, StateStore]:
    raw_store = RawObjectStore(tmp_path)
    return raw_store, StateStore(tmp_path / "data" / "state.sqlite3", raw_store)


def test_raw_write_is_content_addressed_fsynced_and_atomically_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b'{"public":true}'
    digest = hashlib.sha256(payload).hexdigest()
    fsynced: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def observed_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    def observed_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    monkeypatch.setattr(os, "replace", observed_replace)

    stored = RawObjectStore(tmp_path).write(payload, expected_sha256=digest)

    expected_path = tmp_path / "data" / "raw" / "sha256" / digest[:2] / f"{digest}.json"
    assert stored.path == expected_path
    assert stored.sha256 == digest
    assert stored.size_bytes == len(payload)
    assert expected_path.read_bytes() == payload
    assert fsynced
    assert replacements == [(replacements[0][0], expected_path)]
    assert not replacements[0][0].exists()


def test_raw_write_is_idempotent_and_does_not_replace_an_existing_object(tmp_path: Path) -> None:
    store = RawObjectStore(tmp_path)
    payload = b"[]"

    first = store.write(payload)
    first_stat = first.path.stat()
    second = store.write(payload)

    assert second == first
    assert second.path.stat().st_mtime_ns == first_stat.st_mtime_ns


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
    renamed_page = _page(owner="new-owner", name="renamed", next_cursor=8)
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


def test_stage_checkpoints_are_independent_and_return_frozen_records(tmp_path: Path) -> None:
    _, state = _stores(tmp_path)

    enrichment = state.set_stage_checkpoint("enrichment", "repo:7", updated_at=NOW)
    classification = state.set_stage_checkpoint("classification", "repo:3", updated_at=NOW)

    assert state.get_stage_checkpoint("enrichment") == enrichment
    assert state.get_stage_checkpoint("classification") == classification
    with pytest.raises(FrozenInstanceError):
        enrichment.value = "repo:8"  # type: ignore[misc]


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
        "repository_identities",
        "repository_observations",
        "work_item_events",
        "stage_checkpoints",
        "catalog_publications",
    } <= tables
    assert foreign_keys == (0,)  # A new connection must opt in; StateStore itself enables it.
    assert b"secret-token" not in state.path.read_bytes()
    assert state.foreign_keys_enabled is True
    assert state.journal_mode in {"wal", "memory"}
