"""Bounded GitHub public-repository inventory adapter."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import ceil
from types import TracebackType

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
)

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

GITHUB_API_VERSION = "2026-03-10"
_DEFAULT_BASE_URL = "https://api.github.com"
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_ALLOWED_API_HOSTS = frozenset({"api.github.com"})
_DIGITS = re.compile(r"[0-9]+")


class GitHubSourceError(RuntimeError):
    """Safe public error for GitHub source failures."""


class InvalidGitHubResponse(GitHubSourceError):
    """GitHub returned malformed or boundary-invalid data."""


class UnsafeGitHubUrl(GitHubSourceError):
    """A configured or upstream URL escaped the GitHub allowlist."""


class _GitHubOwner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(strict=True, min_length=1)
    id: int = Field(strict=True, gt=0)


class _GitHubInventoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(strict=True, gt=0)
    name: str = Field(strict=True, min_length=1)
    full_name: str = Field(strict=True, min_length=1)
    owner: _GitHubOwner
    html_url: HttpUrl

    @field_validator("html_url")
    @classmethod
    def validate_html_url(cls, value: HttpUrl) -> HttpUrl:
        """Inventory links must identify a repository on GitHub over HTTPS."""

        url = httpx.URL(str(value))
        if url.scheme != "https" or url.host != "github.com" or url.username or url.password:
            raise ValueError("repository URL must use the GitHub HTTPS host")
        return value


_INVENTORY_ADAPTER = TypeAdapter(list[_GitHubInventoryRecord])


class GitHubRepositorySource:
    """Fetch sequential pages from GitHub's public repository inventory."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        token: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("provide either client or transport, not both")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._base_url = _validate_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(transport=transport, timeout=timeout)
        self._token = _validate_token(token)
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._now = now or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        """Describe configuration without exposing authorization material."""

        return (
            f"{type(self).__name__}(base_url={str(self._base_url)!r}, "
            f"authenticated={self._token is not None}, "
            f"max_response_bytes={self._max_response_bytes})"
        )

    def __enter__(self) -> GitHubRepositorySource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close only a client created by this adapter."""

        if self._owns_client:
            self._client.close()

    def fetch_page(self, cursor: int, *, etag: str | None = None) -> RepositoryFetchResult:
        """Fetch and validate one inventory page without waiting or retrying."""

        cursor = _validate_cursor(cursor)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        if etag is not None:
            headers["If-None-Match"] = _validate_etag(etag)

        request_url = self._base_url.copy_with(path="/repositories")
        try:
            with self._client.stream(
                "GET",
                request_url,
                params={"since": str(cursor)},
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                return self._result_from_response(response, etag=etag)
        except GitHubSourceError:
            raise
        except httpx.HTTPError:
            raise GitHubSourceError("GitHub request failed") from None

    def _result_from_response(
        self, response: httpx.Response, *, etag: str | None
    ) -> RepositoryFetchResult:
        if response.status_code in {httpx.codes.FORBIDDEN, httpx.codes.TOO_MANY_REQUESTS}:
            return _build_retry_result(response.status_code, response.headers, self._now)

        rate_limit = _parse_rate_limit(response.headers)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            return UnchangedResult(
                etag=response.headers.get("ETag") or etag,
                rate_limit=rate_limit,
            )
        if response.status_code != httpx.codes.OK:
            raise GitHubSourceError(f"GitHub returned unexpected status {response.status_code}")

        raw_bytes = _read_bounded_body(response, self._max_response_bytes)
        identities = _parse_identities(raw_bytes)
        next_url, next_cursor = _parse_next_link(response)
        return PageResult(
            page=RepositoryPage(
                raw_bytes=raw_bytes,
                raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                etag=response.headers.get("ETag"),
                next_url=next_url,
                next_cursor=next_cursor,
                rate_limit=rate_limit,
                identities=identities,
            )
        )


def _read_bounded_body(response: httpx.Response, max_response_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise InvalidGitHubResponse("invalid GitHub Content-Length header") from None
        if declared_length < 0:
            raise InvalidGitHubResponse("invalid GitHub Content-Length header")
        if declared_length > max_response_bytes:
            raise InvalidGitHubResponse("GitHub response exceeds byte limit")

    body = bytearray()
    for chunk in response.iter_bytes():
        if len(chunk) > max_response_bytes - len(body):
            raise InvalidGitHubResponse("GitHub response exceeds byte limit")
        body.extend(chunk)
    return bytes(body)


def _validate_cursor(cursor: int) -> int:
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    return cursor


def _validate_token(token: str | None) -> str | None:
    if token is None:
        return None
    if not token or token != token.strip() or any(ord(character) < 32 for character in token):
        raise ValueError("invalid GitHub token")
    return token


def _validate_etag(etag: str) -> str:
    if not etag or etag != etag.strip() or any(ord(character) < 32 for character in etag):
        raise ValueError("invalid ETag")
    return etag


def _validate_base_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (TypeError, ValueError):
        raise UnsafeGitHubUrl("GitHub API URL is not allowed") from None
    _ensure_allowed_api_url(url)
    if url.path not in {"", "/"} or url.query or url.fragment:
        raise UnsafeGitHubUrl("GitHub API URL is not allowed")
    return url.copy_with(path="/")


def _ensure_allowed_api_url(url: httpx.URL) -> None:
    if (
        url.scheme != "https"
        or url.host not in _ALLOWED_API_HOSTS
        or url.port not in {None, 443}
        or url.username
        or url.password
    ):
        raise UnsafeGitHubUrl("GitHub API URL is not allowed")


def _parse_identities(raw_bytes: bytes) -> tuple[RepositoryInventoryIdentity, ...]:
    try:
        records = _INVENTORY_ADAPTER.validate_json(raw_bytes)
    except ValidationError:
        raise InvalidGitHubResponse("invalid GitHub repository response") from None

    return tuple(
        RepositoryInventoryIdentity(
            repository_id=record.id,
            name=record.name,
            full_name=record.full_name,
            owner_login=record.owner.login,
            owner_id=record.owner.id,
            html_url=str(record.html_url),
        )
        for record in records
    )


def _parse_next_link(response: httpx.Response) -> tuple[str | None, int | None]:
    link_header = response.headers.get("Link")
    if link_header is None:
        return None, None
    try:
        links = response.links
    except (KeyError, TypeError, ValueError):
        raise InvalidGitHubResponse("invalid GitHub Link header") from None
    if not links:
        raise InvalidGitHubResponse("invalid GitHub Link header")
    next_link = links.get("next")
    if next_link is None:
        return None, None
    raw_url = next_link.get("url")
    if not isinstance(raw_url, str):
        raise InvalidGitHubResponse("invalid GitHub Link header")
    try:
        url = httpx.URL(raw_url)
    except (TypeError, ValueError):
        raise UnsafeGitHubUrl("GitHub next URL is not allowed") from None
    _ensure_allowed_api_url(url)
    if url.path != "/repositories" or url.fragment:
        raise UnsafeGitHubUrl("GitHub next URL is not allowed")
    since_values = url.params.get_list("since")
    if len(since_values) != 1 or _DIGITS.fullmatch(since_values[0]) is None:
        raise UnsafeGitHubUrl("GitHub next URL has no numeric cursor")
    return str(url), int(since_values[0])


def _parse_rate_limit(
    headers: httpx.Headers, *, ignore_invalid_reset: bool = False
) -> RateLimitFacts:
    return RateLimitFacts(
        limit=_parse_nonnegative_header(headers, "X-RateLimit-Limit"),
        remaining=_parse_nonnegative_header(headers, "X-RateLimit-Remaining"),
        reset_epoch=_parse_reset_header(headers, ignore_invalid=ignore_invalid_reset),
        resource=headers.get("X-RateLimit-Resource"),
    )


def _parse_nonnegative_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    value = value.strip()
    if _DIGITS.fullmatch(value) is None:
        raise InvalidGitHubResponse(f"invalid {name} header")
    try:
        return int(value)
    except ValueError:
        raise InvalidGitHubResponse(f"invalid {name} header") from None


def _parse_reset_header(headers: httpx.Headers, *, ignore_invalid: bool) -> int | None:
    try:
        return _parse_nonnegative_header(headers, "X-RateLimit-Reset")
    except InvalidGitHubResponse:
        if ignore_invalid:
            return None
        raise


def _build_retry_result(
    status_code: int,
    headers: httpx.Headers,
    now: Callable[[], datetime],
) -> RetryResult:
    current = now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise GitHubSourceError("retry clock must be timezone-aware")
    current = current.astimezone(UTC)

    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        parsed = _parse_retry_after(retry_after, current)
        if parsed is not None:
            delay, retry_at = parsed
            return RetryResult(
                status_code=status_code,
                decision=RetryDecision(
                    source=RetrySource.RETRY_AFTER,
                    delay_seconds=delay,
                    retry_at=retry_at,
                ),
                rate_limit=_parse_rate_limit(headers, ignore_invalid_reset=True),
            )

    rate_limit = _parse_rate_limit(headers)
    if rate_limit.reset_epoch is not None:
        try:
            retry_at = datetime.fromtimestamp(rate_limit.reset_epoch, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise InvalidGitHubResponse("invalid GitHub retry timing") from None
        delay, retry_at = _retry_timing(retry_at, current)
        return RetryResult(
            status_code=status_code,
            decision=RetryDecision(
                source=RetrySource.RATE_LIMIT_RESET,
                delay_seconds=delay,
                retry_at=retry_at,
            ),
            rate_limit=rate_limit,
        )
    raise GitHubSourceError("GitHub response has no usable retry timing")


def _parse_retry_after(value: str, current: datetime) -> tuple[int, datetime] | None:
    value = value.strip()
    if _DIGITS.fullmatch(value) is not None:
        try:
            delay = int(value)
            return delay, current + timedelta(seconds=delay)
        except (OverflowError, ValueError):
            raise InvalidGitHubResponse("invalid GitHub retry timing") from None
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None or retry_at.utcoffset() is None:
        return None
    retry_at = retry_at.astimezone(UTC)
    return _retry_timing(retry_at, current)


def _retry_timing(retry_at: datetime, current: datetime) -> tuple[int, datetime]:
    if retry_at <= current:
        return 0, current
    return ceil((retry_at - current).total_seconds()), retry_at
