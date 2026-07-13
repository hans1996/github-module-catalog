from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from github_module_catalog.github import (
    GITHUB_API_VERSION,
    GitHubRepositorySource,
    GitHubSourceError,
    InvalidGitHubResponse,
    UnsafeGitHubUrl,
)
from github_module_catalog.source import (
    PageResult,
    RetryResult,
    RetrySource,
    UnchangedResult,
)

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


class ChunkedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_read = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


def inventory_record(**updates: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": 42,
        "name": "module-catalog",
        "full_name": "octocat/module-catalog",
        "owner": {"login": "octocat", "id": 1},
        "html_url": "https://github.com/octocat/module-catalog",
    }
    record.update(updates)
    return record


class TrackingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=[inventory_record()])

    def close(self) -> None:
        self.closed = True


def test_fetches_inventory_page_with_cursor_headers_and_source_facts() -> None:
    raw_bytes = json.dumps([inventory_record()], separators=(",", ":")).encode()
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            content=raw_bytes,
            headers={
                "ETag": '"inventory-v1"',
                "Link": '<https://api.github.com/repositories?since=42>; rel="next"',
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "57",
                "X-RateLimit-Reset": "1783930500",
                "X-RateLimit-Resource": "core",
            },
        )

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler), now=lambda: NOW)
    result = source.fetch_page(41)

    assert isinstance(result, PageResult)
    assert result.page.raw_bytes == raw_bytes
    assert result.page.raw_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert result.page.etag == '"inventory-v1"'
    assert result.page.next_url == "https://api.github.com/repositories?since=42"
    assert result.page.next_cursor == 42
    assert result.page.rate_limit.limit == 60
    assert result.page.rate_limit.remaining == 57
    assert result.page.rate_limit.reset_epoch == 1783930500
    assert result.page.rate_limit.resource == "core"
    assert result.page.identities[0].repository_id == 42
    assert result.page.identities[0].name == "module-catalog"
    assert result.page.identities[0].full_name == "octocat/module-catalog"
    assert result.page.identities[0].owner_login == "octocat"
    assert result.page.identities[0].owner_id == 1
    assert result.page.identities[0].html_url == "https://github.com/octocat/module-catalog"
    assert seen_request is not None
    assert seen_request.url.path == "/repositories"
    assert dict(seen_request.url.params) == {"since": "41"}
    assert seen_request.headers["Accept"] == "application/vnd.github+json"
    assert seen_request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    assert "Authorization" not in seen_request.headers


def test_inventory_records_may_omit_enrichment_only_topics_and_license() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[inventory_record()])

    result = GitHubRepositorySource(transport=httpx.MockTransport(handler)).fetch_page(0)

    assert isinstance(result, PageResult)
    assert len(result.page.identities) == 1
    assert result.page.observations == ()


def test_complete_inventory_metadata_becomes_a_validated_observation() -> None:
    record = inventory_record(
        description="A reusable CLI catalog",
        language="Python",
        created_at="2026-07-01T08:00:00Z",
        updated_at="2026-07-11T08:00:00Z",
        pushed_at="2026-07-12T08:00:00Z",
        archived=False,
        disabled=False,
        fork=False,
        topics=["CLI", "catalog"],
        license={"spdx_id": "MIT", "name": "MIT License"},
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[record])

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: NOW
    ).fetch_page(0)

    assert isinstance(result, PageResult)
    assert len(result.page.observations) == 1
    observation = result.page.observations[0]
    assert observation.identity.repository_id == 42
    assert observation.description == "A reusable CLI catalog"
    assert observation.primary_language == "Python"
    assert observation.topics == ("catalog", "cli")
    assert observation.license_spdx == "MIT"
    assert observation.license_name == "MIT License"
    assert observation.observed_at == NOW


@pytest.mark.parametrize(
    "updates",
    [
        {"description": "partial metadata must not be ignored"},
        {
            "description": "invalid lifecycle flag",
            "language": "Python",
            "created_at": "2026-07-01T08:00:00Z",
            "updated_at": "2026-07-11T08:00:00Z",
            "pushed_at": None,
            "archived": "false",
            "disabled": False,
            "fork": False,
        },
    ],
)
def test_present_but_invalid_inventory_metadata_fails_closed(updates: dict[str, Any]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[inventory_record(**updates)])

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler), now=lambda: NOW)

    with pytest.raises(InvalidGitHubResponse, match="invalid GitHub repository response"):
        source.fetch_page(0)


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_response_limit_rejects_chunk_overflow_without_trusting_content_length(
    headers: dict[str, str],
) -> None:
    stream = ChunkedStream([b"1234", b"5678", b"must-not-be-read"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream)

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler), max_response_bytes=5)

    with pytest.raises(InvalidGitHubResponse, match="response exceeds byte limit"):
        source.fetch_page(0)
    assert stream.chunks_read == 2


@pytest.mark.parametrize("max_response_bytes", [0, -1, True])
def test_response_limit_must_be_a_positive_integer(max_response_bytes: int) -> None:
    with pytest.raises(ValueError, match="max_response_bytes must be a positive integer"):
        GitHubRepositorySource(max_response_bytes=max_response_bytes)


def test_invalid_token_is_rejected_before_owned_client_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_constructed = False

    def client_factory(*args: object, **kwargs: object) -> httpx.Client:
        nonlocal client_constructed
        client_constructed = True
        raise AssertionError("client must not be constructed")

    monkeypatch.setattr(httpx, "Client", client_factory)
    invalid_credential = "line-one\nline-two"

    with pytest.raises(ValueError, match="invalid GitHub token"):
        GitHubRepositorySource(token=invalid_credential)
    assert not client_constructed


def test_context_manager_closes_only_an_owned_client() -> None:
    transport = TrackingTransport()

    with GitHubRepositorySource(transport=transport) as source:
        result = source.fetch_page(0)
        assert isinstance(result, PageResult)
        assert not transport.closed

    assert transport.closed


def test_context_manager_leaves_an_injected_client_open() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[inventory_record()])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with GitHubRepositorySource(client=client) as source:
            result = source.fetch_page(0)
            assert isinstance(result, PageResult)
        assert not client.is_closed
    finally:
        client.close()


def test_link_next_relation_is_the_only_pagination_signal() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[inventory_record()],
            headers={
                "Link": (
                    '<https://api.github.com/repositories?since=101>; rel="next", '
                    '<https://api.github.com/repositories?since=999>; rel="last"'
                )
            },
        )

    result = GitHubRepositorySource(transport=httpx.MockTransport(handler)).fetch_page(0)

    assert isinstance(result, PageResult)
    assert result.page.next_cursor == 101
    assert result.page.next_url == "https://api.github.com/repositories?since=101"


@pytest.mark.parametrize("status_code", [403, 429])
def test_retry_after_takes_precedence_over_rate_limit_reset(status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={
                "Retry-After": "120",
                "X-RateLimit-Reset": str(int(NOW.timestamp()) + 900),
                "X-RateLimit-Remaining": "0",
            },
        )

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: NOW
    ).fetch_page(0)

    assert isinstance(result, RetryResult)
    assert result.decision.source is RetrySource.RETRY_AFTER
    assert result.decision.delay_seconds == 120
    assert result.decision.retry_at.timestamp() == NOW.timestamp() + 120
    assert result.rate_limit.remaining == 0


def test_rate_limit_reset_is_used_when_retry_after_is_absent() -> None:
    reset_epoch = int(NOW.timestamp()) + 75

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RateLimit-Reset": str(reset_epoch)})

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: NOW
    ).fetch_page(0)

    assert isinstance(result, RetryResult)
    assert result.decision.source is RetrySource.RATE_LIMIT_RESET
    assert result.decision.delay_seconds == 75
    assert result.decision.retry_at.timestamp() == reset_epoch


def test_valid_retry_after_precedes_a_malformed_rate_limit_reset() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "30", "X-RateLimit-Reset": "not-an-epoch"},
        )

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: NOW
    ).fetch_page(0)

    assert isinstance(result, RetryResult)
    assert result.decision.source is RetrySource.RETRY_AFTER
    assert result.decision.delay_seconds == 30
    assert result.rate_limit.reset_epoch is None


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": str(10**20)},
        {"Retry-After": "Fri, 31 Dec 9999 23:59:59 -2359"},
        {"X-RateLimit-Reset": str(10**20)},
    ],
)
def test_extreme_retry_timestamps_fail_closed(headers: dict[str, str]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler), now=lambda: NOW)

    with pytest.raises(InvalidGitHubResponse, match="invalid GitHub retry timing"):
        source.fetch_page(0)


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": format_datetime(datetime.fromtimestamp(1002, tz=UTC), usegmt=True)},
        {"X-RateLimit-Reset": "1002"},
    ],
)
def test_fractional_positive_retry_delay_rounds_up(headers: dict[str, str]) -> None:
    current = datetime.fromtimestamp(1000.25, tz=UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: current
    ).fetch_page(0)

    assert isinstance(result, RetryResult)
    assert result.decision.delay_seconds == 2
    assert result.decision.retry_at == datetime.fromtimestamp(1002, tz=UTC)


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": format_datetime(datetime.fromtimestamp(999, tz=UTC), usegmt=True)},
        {"X-RateLimit-Reset": "999"},
    ],
)
def test_past_retry_timestamp_clamps_to_current_time(headers: dict[str, str]) -> None:
    current = datetime.fromtimestamp(1000.25, tz=UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    result = GitHubRepositorySource(
        transport=httpx.MockTransport(handler), now=lambda: current
    ).fetch_page(0)

    assert isinstance(result, RetryResult)
    assert result.decision.delay_seconds == 0
    assert result.decision.retry_at == current


def test_etag_is_sent_and_304_returns_typed_unchanged_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["If-None-Match"] == '"inventory-v1"'
        return httpx.Response(
            304,
            headers={"ETag": '"inventory-v1"', "X-RateLimit-Remaining": "55"},
        )

    result = GitHubRepositorySource(transport=httpx.MockTransport(handler)).fetch_page(
        42, etag='"inventory-v1"'
    )

    assert isinstance(result, UnchangedResult)
    assert result.etag == '"inventory-v1"'
    assert result.rate_limit.remaining == 55


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"id": 42}).encode(),
        json.dumps([inventory_record(id=True)]).encode(),
        json.dumps([inventory_record(id=-1)]).encode(),
        json.dumps([inventory_record(owner={"login": "octocat"})]).encode(),
        json.dumps([inventory_record(html_url="not-a-url")]).encode(),
    ],
)
def test_malformed_json_and_invalid_inventory_records_fail_closed(payload: bytes) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler))

    with pytest.raises(InvalidGitHubResponse, match="invalid GitHub repository response"):
        source.fetch_page(0)


def test_token_is_sent_but_redacted_from_repr_and_exceptions() -> None:
    redaction_marker = "redaction-test-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {redaction_marker}"
        return httpx.Response(500, text=f"upstream echoed {redaction_marker}")

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler), token=redaction_marker)

    assert redaction_marker not in repr(source)
    with pytest.raises(GitHubSourceError) as raised:
        source.fetch_page(0)
    assert redaction_marker not in str(raised.value)
    assert redaction_marker not in repr(raised.value)


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/repositories?since=43",
        "http://api.github.com/repositories?since=43",
        "https://api.github.com/user?since=43",
        "https://api.github.com/repositories?since=not-numeric",
    ],
)
def test_malicious_or_invalid_next_link_is_rejected(next_url: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[inventory_record()],
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    source = GitHubRepositorySource(transport=httpx.MockTransport(handler))

    with pytest.raises(UnsafeGitHubUrl):
        source.fetch_page(0)


def test_base_url_is_restricted_to_allowlisted_https_github_host() -> None:
    with pytest.raises(UnsafeGitHubUrl):
        GitHubRepositorySource(base_url="https://evil.example")


def test_page_and_result_models_are_frozen() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[inventory_record()])

    result = GitHubRepositorySource(transport=httpx.MockTransport(handler)).fetch_page(0)

    assert isinstance(result, PageResult)
    with pytest.raises(FrozenInstanceError):
        result.page.next_cursor = 999  # type: ignore[misc]
