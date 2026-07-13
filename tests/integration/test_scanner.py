"""Integration tests for resumable discovery orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import HttpUrl

from github_module_catalog.catalog import build_catalog_from_state
from github_module_catalog.models import RepositoryIdentity, RepositoryObservation, ReuseStatus
from github_module_catalog.scanner import DiscoveryScanner, ScanOutcome, ScanStatus
from github_module_catalog.source import (
    PageResult,
    RateLimitFacts,
    RepositoryFetchResult,
    RepositoryInventoryIdentity,
    RepositoryPage,
    RetryDecision,
    RetryResult,
    RetrySource,
    UnchangedResult,
)
from github_module_catalog.state import StateStore
from github_module_catalog.storage import RawObjectStore
from github_module_catalog.taxonomy import load_taxonomy

NOW = datetime(2026, 7, 13, tzinfo=UTC)


@dataclass
class FakeSource:
    outcomes: list[RepositoryFetchResult | Exception]
    cursors: list[int] = field(default_factory=list)

    def fetch_page(self, cursor: int, *, etag: str | None = None) -> RepositoryFetchResult:
        del etag
        self.cursors.append(cursor)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _page(*repository_ids: int) -> RepositoryPage:
    payload = [
        {
            "id": repository_id,
            "name": f"repo-{repository_id}",
            "full_name": f"octocat/repo-{repository_id}",
        }
        for repository_id in repository_ids
    ]
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode()
    identities = tuple(
        RepositoryInventoryIdentity(
            repository_id=repository_id,
            name=f"repo-{repository_id}",
            full_name=f"octocat/repo-{repository_id}",
            owner_login="octocat",
            owner_id=1,
            html_url=f"https://github.com/octocat/repo-{repository_id}",
        )
        for repository_id in repository_ids
    )
    return RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        etag=None,
        next_url=f"https://api.github.com/repositories?since={max(repository_ids)}",
        next_cursor=max(repository_ids),
        rate_limit=RateLimitFacts(remaining=50),
        identities=identities,
    )


def _empty_page() -> RepositoryPage:
    raw_bytes = b"[]"
    return RepositoryPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        etag=None,
        next_url=None,
        next_cursor=None,
        rate_limit=RateLimitFacts(remaining=50),
        identities=(),
    )


def _observation(repository_id: int) -> RepositoryObservation:
    return RepositoryObservation(
        identity=RepositoryIdentity(repository_id=repository_id),
        owner="octocat",
        name=f"repo-{repository_id}",
        full_name=f"octocat/repo-{repository_id}",
        html_url=HttpUrl(f"https://github.com/octocat/repo-{repository_id}"),
        description="A reusable CLI catalog",
        topics=("cli",),
        primary_language="Python",
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=2),
        pushed_at=NOW - timedelta(days=1),
        observed_at=NOW,
        archived=False,
        disabled=False,
        fork=False,
        license_spdx=None,
        license_name=None,
    )


def _stores(tmp_path: Path) -> tuple[RawObjectStore, StateStore]:
    raw_store = RawObjectStore(tmp_path)
    return raw_store, StateStore(tmp_path / "data" / "state.sqlite3", raw_store)


def _scan(
    source: FakeSource,
    raw_store: RawObjectStore,
    state: StateStore,
    *,
    max_pages: int,
) -> ScanOutcome:
    return DiscoveryScanner(
        source=source,
        raw_store=raw_store,
        state=state,
        source_name="github-public-repositories",
    ).scan(max_pages=max_pages, started_at=NOW)


def test_scan_resumes_at_source_scoped_committed_cursor_and_honors_page_limit(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    first_source = FakeSource([PageResult(_page(2, 7)), PageResult(_page(9))])

    first = _scan(first_source, raw_store, state, max_pages=2)
    second_source = FakeSource([PageResult(_page(11))])
    second = _scan(second_source, raw_store, state, max_pages=1)

    assert first.status == ScanStatus.PAGE_LIMIT_REACHED
    assert (first.cursor_start, first.cursor_end, first.pages_committed) == (0, 9, 2)
    assert first_source.cursors == [0, 7]
    assert (second.cursor_start, second.cursor_end) == (9, 11)
    assert second_source.cursors == [9]
    assert [item.repository_id for item in state.list_repository_identities()] == [2, 7, 9, 11]
    with pytest.raises(ValueError, match="max_pages"):
        _scan(FakeSource([]), raw_store, state, max_pages=0)


def test_empty_and_terminal_pages_return_completed_without_refetching(tmp_path: Path) -> None:
    raw_store, state = _stores(tmp_path)
    empty_source = FakeSource([PageResult(_empty_page()), PageResult(_empty_page())])

    empty = _scan(empty_source, raw_store, state, max_pages=3)
    polled_again = _scan(empty_source, raw_store, state, max_pages=3)
    terminal = _scan(
        FakeSource([PageResult(replace(_page(7), next_url=None))]),
        raw_store,
        state,
        max_pages=3,
    )

    assert empty.status == ScanStatus.COMPLETED
    assert (empty.cursor_start, empty.cursor_end, empty.pages_committed) == (0, 0, 1)
    assert polled_again.status == ScanStatus.COMPLETED
    assert (polled_again.cursor_start, polled_again.cursor_end) == (0, 0)
    assert empty_source.cursors == [0, 0]
    assert terminal.status == ScanStatus.COMPLETED
    assert (terminal.cursor_start, terminal.cursor_end, terminal.pages_committed) == (0, 7, 1)


def test_existing_page_replay_does_not_increment_committed_page_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_store, state = _stores(tmp_path)
    page = replace(_page(7), next_url=None)
    run = state.create_crawl_run("github-public-repositories", started_at=NOW)
    raw_store.write(page.raw_bytes, expected_sha256=page.raw_sha256)
    state.commit_discovery_page(run.id, cursor_before=0, page=page, committed_at=NOW)
    monkeypatch.setattr(state, "create_crawl_run", lambda *_args, **_kwargs: run)

    result = _scan(FakeSource([PageResult(page)]), raw_store, state, max_pages=1)

    assert result.status == ScanStatus.COMPLETED
    assert (result.cursor_start, result.cursor_end, result.pages_committed) == (0, 7, 0)


def test_interrupted_page_is_refetched_without_duplicate_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_store, state = _stores(tmp_path)
    page = _page(7)
    original_commit = state.commit_discovery_page
    attempts = 0

    def interrupted_commit(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database interruption")
        return original_commit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(state, "commit_discovery_page", interrupted_commit)
    first_source = FakeSource([PageResult(page)])
    first = _scan(first_source, raw_store, state, max_pages=1)
    second_source = FakeSource([PageResult(page)])
    second = _scan(second_source, raw_store, state, max_pages=1)

    assert first.status == ScanStatus.ERROR
    assert (first.cursor_start, first.cursor_end) == (0, 0)
    assert raw_store.read(page.raw_sha256) == page.raw_bytes
    assert second.status == ScanStatus.PAGE_LIMIT_REACHED
    assert first_source.cursors == second_source.cursors == [0]
    assert [item.repository_id for item in state.list_repository_identities()] == [7]


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (UnchangedResult(None, RateLimitFacts()), ScanStatus.UNCHANGED),
        (
            RetryResult(
                429,
                RetryDecision(RetrySource.RETRY_AFTER, 60, NOW + timedelta(seconds=60)),
                RateLimitFacts(remaining=0),
            ),
            ScanStatus.RETRY,
        ),
    ],
)
def test_retry_and_unchanged_outcomes_do_not_advance_cursor(
    tmp_path: Path,
    outcome: RepositoryFetchResult,
    expected_status: ScanStatus,
) -> None:
    raw_store, state = _stores(tmp_path)

    result = _scan(FakeSource([outcome]), raw_store, state, max_pages=1)

    assert result.status == expected_status
    assert (result.cursor_start, result.cursor_end, result.pages_committed) == (0, 0, 0)


def test_source_error_is_typed_and_enrichment_failures_do_not_block_discovery(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    error = _scan(
        FakeSource([RuntimeError("secret upstream detail")]), raw_store, state, max_pages=1
    )
    _scan(FakeSource([PageResult(_page(7))]), raw_store, state, max_pages=1)
    state.append_work_item_event(7, "enrichment", "retry", occurred_at=NOW)
    continued = _scan(FakeSource([PageResult(_page(8))]), raw_store, state, max_pages=1)

    assert error.status == ScanStatus.ERROR
    assert error.error_type == "RuntimeError"
    assert "secret upstream detail" not in repr(error)
    assert continued.cursor_end == 8
    assert [item.repository_id for item in state.list_repository_identities()] == [7, 8]


def test_durable_discovery_records_inventory_observation_for_classification(
    tmp_path: Path,
) -> None:
    raw_store, state = _stores(tmp_path)
    observation = _observation(7)
    page = replace(_page(7), next_url=None, observations=(observation,))

    result = _scan(FakeSource([PageResult(page)]), raw_store, state, max_pages=1)
    manifest = build_catalog_from_state(
        state,
        taxonomy=load_taxonomy(Path("config/taxonomy.yaml")),
        source="github-public-repositories",
    )

    assert result.status == ScanStatus.COMPLETED
    assert result.observations_recorded == 1
    assert result.observation_failures == 0
    assert state.list_latest_repository_observations() == (observation,)
    binding = state.catalog_snapshot("github-public-repositories").observation_bindings[0]
    assert binding.repository_id == 7
    assert binding.observation_hash == observation.stable_hash()
    assert binding.raw_sha256 == page.raw_sha256
    assert binding.observed_at == observation.observed_at
    assert manifest.validated_observation_count == 1
    assert manifest.entries[0].assertions[0].capability_id == "cli"
    assert manifest.entries[0].repository.reuse_status is ReuseStatus.DISCOVERY_ONLY
    assert state.catalog_snapshot("github-public-repositories").pending_count == 0


def test_observation_recording_failure_isolated_after_cursor_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_store, state = _stores(tmp_path)
    page = replace(_page(7), next_url=None, observations=(_observation(7),))

    def fail_recording(*_: object) -> None:
        raise RuntimeError("sensitive failure details")

    monkeypatch.setattr(state, "record_discovery_observation", fail_recording)

    result = _scan(FakeSource([PageResult(page)]), raw_store, state, max_pages=1)

    assert result.status == ScanStatus.COMPLETED
    assert result.cursor_end == 7
    assert result.observations_recorded == 0
    assert result.observation_failures == 1
    assert state.list_latest_repository_observations() == ()
    events = state.list_work_item_events(repository_id=7, stage="enrichment")
    assert tuple(event.event for event in events) == ("queued", "retry")
    assert "sensitive failure details" not in repr(events)
    assert state.catalog_snapshot("github-public-repositories").retry_count == 1
