"""Integration tests for ranked GitHub repository Search snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from github_module_catalog.github_search import (
    GitHubSearchError,
    GitHubSearchSource,
    InvalidGitHubSearchResponse,
    UnsafeGitHubSearchUrl,
    build_github_search_query,
)
from github_module_catalog.models import CatalogSelectionCriteria
from github_module_catalog.storage import RawObjectStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
CUTOFF = datetime(2025, 7, 13, tzinfo=UTC)


def _credential_marker() -> str:
    return "test-only-auth-marker"


@pytest.fixture
def raw_store(tmp_path: Path) -> Iterator[RawObjectStore]:
    store = RawObjectStore(tmp_path)
    try:
        yield store
    finally:
        store.close()


def _criteria(*, result_limit: int = 100) -> CatalogSelectionCriteria:
    return CatalogSelectionCriteria(
        min_stars=100,
        pushed_since=CUTOFF,
        result_limit=result_limit,
    )


def _item(repository_id: int, **overrides: Any) -> dict[str, object]:
    values: dict[str, object] = {
        "id": repository_id,
        "name": f"repo-{repository_id}",
        "full_name": f"example/repo-{repository_id}",
        "owner": {"login": "example", "id": 1},
        "html_url": f"https://github.com/example/repo-{repository_id}",
        "description": "Untrusted [description](javascript:alert(1))",
        "topics": ["cli", "python"],
        "language": "Python",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2026-07-12T00:00:00Z",
        "pushed_at": "2026-07-12T00:00:00Z",
        "stargazers_count": 1_000,
        "archived": False,
        "disabled": False,
        "fork": False,
        "private": False,
        "license": {"spdx_id": "MIT", "name": "MIT License"},
    }
    return values | overrides


def _body(
    items: list[dict[str, object]],
    *,
    total_count: int | object | None = None,
    incomplete_results: bool | object = False,
) -> bytes:
    document = {
        "total_count": len(items) if total_count is None else total_count,
        "incomplete_results": incomplete_results,
        "items": items,
    }
    return json.dumps(document, separators=(",", ":")).encode()


def test_collect_snapshot_uses_canonical_query_headers_raw_hash_and_restarts_page_one(
    raw_store: RawObjectStore,
) -> None:
    raw_bytes = _body([_item(7)], total_count=1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=raw_bytes)

    source = GitHubSearchSource(
        transport=httpx.MockTransport(handler),
        token=_credential_marker(),
        now=lambda: NOW,
    )
    try:
        first = source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
        second = source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()

    assert build_github_search_query(_criteria()) == (
        "stars:>=100 pushed:>=2025-07-13 archived:false is:public"
    )
    assert [request.url.params["page"] for request in requests] == ["1", "1"]
    assert all(request.url.path == "/search/repositories" for request in requests)
    assert all(request.url.params["sort"] == "stars" for request in requests)
    assert all(request.url.params["order"] == "desc" for request in requests)
    assert all(request.url.params["per_page"] == "100" for request in requests)
    assert all(
        request.headers["Authorization"] == f"Bearer {_credential_marker()}" for request in requests
    )
    assert all(request.headers["Accept"] == "application/vnd.github+json" for request in requests)
    assert _credential_marker() not in repr(source)

    expected_hash = hashlib.sha256(raw_bytes).hexdigest()
    assert first.raw_page_hashes == (expected_hash,)
    assert raw_store.read(expected_hash) == raw_bytes
    assert first == second
    assert first.observed_at == NOW
    assert first.api_total_count == 1
    assert first.pages_fetched == 1
    assert first.result_limit == 100
    assert first.repository_ranks == ((7, 1),)
    assert first.observations[0].stargazers_count == 1_000
    assert first.observations[0].private is False


def test_collect_snapshot_fetches_bounded_pages_deduplicates_and_sorts_ties(
    raw_store: RawObjectStore,
) -> None:
    first_items = [_item(index, stargazers_count=1_000 - index) for index in range(1, 101)]
    second_items = [
        _item(100, stargazers_count=900),
        _item(501, stargazers_count=2_000),
        _item(502, stargazers_count=2_000),
        *[_item(index, stargazers_count=800 - index) for index in range(503, 600)],
    ]
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        items = first_items if page == 1 else second_items
        return httpx.Response(200, content=_body(items, total_count=2_500))

    source = GitHubSearchSource(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        snapshot = source.collect_snapshot(
            _criteria(result_limit=150),
            max_pages=2,
            raw_store=raw_store,
        )
    finally:
        source.close()

    assert requested_pages == [1, 2]
    assert snapshot.pages_fetched == 2
    assert len(snapshot.raw_page_hashes) == 2
    assert len(snapshot.observations) == 150
    assert snapshot.repository_ranks == tuple(
        (observation.identity.repository_id, rank)
        for rank, observation in enumerate(snapshot.observations, start=1)
    )
    assert [item.identity.repository_id for item in snapshot.observations[:2]] == [501, 502]
    observed_stars = [item.stargazers_count for item in snapshot.observations]
    assert all(stars is not None for stars in observed_stars)
    assert observed_stars == sorted(
        (stars for stars in observed_stars if stars is not None),
        reverse=True,
    )
    assert sum(item.identity.repository_id == 100 for item in snapshot.observations) == 1


def test_duplicate_pages_must_still_fill_the_requested_unique_window(
    raw_store: RawObjectStore,
) -> None:
    first_items = [_item(index) for index in range(1, 101)]
    second_items = [_item(100), *[_item(index) for index in range(101, 200)]]

    def handler(request: httpx.Request) -> httpx.Response:
        items = first_items if request.url.params["page"] == "1" else second_items
        return httpx.Response(200, content=_body(items, total_count=200))

    source = GitHubSearchSource(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="unique repositories"):
            source.collect_snapshot(
                _criteria(result_limit=200),
                max_pages=2,
                raw_store=raw_store,
            )
    finally:
        source.close()


def test_conflicting_duplicate_repository_facts_abort_even_with_backfill_capacity(
    raw_store: RawObjectStore,
) -> None:
    first_items = [_item(index) for index in range(1, 101)]
    second_items = [
        _item(100, stargazers_count=9_999),
        *[_item(index) for index in range(101, 200)],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        items = first_items if request.url.params["page"] == "1" else second_items
        return httpx.Response(200, content=_body(items, total_count=2_500))

    source = GitHubSearchSource(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="conflicting duplicate"):
            source.collect_snapshot(
                _criteria(result_limit=150),
                max_pages=2,
                raw_store=raw_store,
            )
    finally:
        source.close()


def test_observation_time_is_captured_after_each_response(
    raw_store: RawObjectStore,
) -> None:
    before_request = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    after_response = datetime(2026, 7, 13, 12, 0, 2, tzinfo=UTC)
    current_time = before_request

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal current_time
        current_time = after_response
        return httpx.Response(
            200,
            content=_body(
                [
                    _item(
                        7,
                        updated_at="2026-07-13T12:00:01Z",
                        pushed_at="2026-07-13T12:00:01Z",
                    )
                ],
                total_count=1,
            ),
        )

    source = GitHubSearchSource(
        transport=httpx.MockTransport(handler),
        now=lambda: current_time,
    )
    try:
        snapshot = source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()

    assert snapshot.observed_at == after_response
    assert snapshot.observations[0].observed_at == after_response


@pytest.mark.parametrize(
    "override",
    [
        {"stargazers_count": 99},
        {"pushed_at": "2025-07-12T23:59:59Z"},
        {"archived": True},
        {"fork": True},
        {"private": True},
    ],
)
def test_collect_snapshot_rejects_every_locally_ineligible_repository_fact(
    raw_store: RawObjectStore,
    override: dict[str, object],
) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=_body([_item(7, **override)], total_count=1),
            )
        ),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="selection policy"):
            source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()


@pytest.mark.parametrize("failed_page", [1, 2])
def test_incomplete_results_on_any_page_abort_the_snapshot(
    raw_store: RawObjectStore,
    failed_page: int,
) -> None:
    items = [_item(index) for index in range(1, 101)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            content=_body(
                items,
                total_count=200,
                incomplete_results=page == failed_page,
            ),
        )

    source = GitHubSearchSource(transport=httpx.MockTransport(handler), now=lambda: NOW)
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="incomplete"):
            source.collect_snapshot(
                _criteria(result_limit=200),
                max_pages=2,
                raw_store=raw_store,
            )
    finally:
        source.close()


def test_total_count_drift_or_early_short_page_aborts_without_partial_snapshot(
    raw_store: RawObjectStore,
) -> None:
    calls = 0

    def drifting_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        total_count = 200 if calls == 1 else 201
        return httpx.Response(
            200,
            content=_body(
                [_item(calls * 1_000 + index) for index in range(100)], total_count=total_count
            ),
        )

    source = GitHubSearchSource(transport=httpx.MockTransport(drifting_handler), now=lambda: NOW)
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="total_count changed"):
            source.collect_snapshot(
                _criteria(result_limit=200),
                max_pages=2,
                raw_store=raw_store,
            )
    finally:
        source.close()

    short_source = GitHubSearchSource(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=_body([_item(index) for index in range(1, 100)], total_count=200),
            )
        ),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="short page"):
            short_source.collect_snapshot(
                _criteria(result_limit=200),
                max_pages=2,
                raw_store=raw_store,
            )
    finally:
        short_source.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(
            200,
            content=_body([_item(7)], total_count="1"),
        ),
        httpx.Response(
            200,
            content=_body([_item(7)], incomplete_results="false"),
        ),
    ],
)
def test_malformed_search_envelopes_are_rejected(
    raw_store: RawObjectStore,
    response: httpx.Response,
) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(lambda _request: response),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="invalid GitHub Search response"):
            source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_response_body_limit_does_not_trust_content_length(
    raw_store: RawObjectStore,
    headers: dict[str, str],
) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, headers=headers, content=b"123456")
        ),
        max_response_bytes=5,
        now=lambda: NOW,
    )
    try:
        with pytest.raises(InvalidGitHubSearchResponse, match="byte limit"):
            source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()


def test_non_success_errors_and_repr_never_leak_the_token(raw_store: RawObjectStore) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, content=b"secret")),
        token=_credential_marker(),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(GitHubSearchError) as captured:
            source.collect_snapshot(_criteria(), max_pages=1, raw_store=raw_store)
    finally:
        source.close()

    assert _credential_marker() not in str(captured.value)
    assert _credential_marker() not in repr(source)


@pytest.mark.parametrize("max_pages", [0, 11, True])
def test_page_budget_is_a_strict_one_to_ten(
    raw_store: RawObjectStore,
    max_pages: object,
) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=_body([]))),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(ValueError, match="max_pages"):
            source.collect_snapshot(
                _criteria(),
                max_pages=max_pages,  # type: ignore[arg-type]
                raw_store=raw_store,
            )
    finally:
        source.close()


def test_result_limit_must_fit_the_page_budget(raw_store: RawObjectStore) -> None:
    source = GitHubSearchSource(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=_body([]))),
        now=lambda: NOW,
    )
    try:
        with pytest.raises(ValueError, match="result_limit"):
            source.collect_snapshot(
                _criteria(result_limit=101),
                max_pages=1,
                raw_store=raw_store,
            )
    finally:
        source.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.github.com",
        "https://evil.test",
        "https://api.github.com/search/repositories",
        "https://user:pass@api.github.com",
    ],
)
def test_source_rejects_unsafe_api_base_urls(base_url: str) -> None:
    with pytest.raises(UnsafeGitHubSearchUrl):
        GitHubSearchSource(base_url=base_url)


@pytest.mark.parametrize("token", ["", " leading", "trailing ", "line\nbreak"])
def test_source_rejects_invalid_tokens(token: str) -> None:
    with pytest.raises(ValueError, match="token"):
        GitHubSearchSource(token=token)
