"""Bounded, all-or-nothing GitHub repository Search snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from types import TracebackType

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    ValidationError,
)

from github_module_catalog.github import GITHUB_API_VERSION
from github_module_catalog.models import (
    CatalogSelectionCriteria,
    RepositoryIdentity,
    RepositoryObservation,
)
from github_module_catalog.storage import RawObjectStore

_DEFAULT_BASE_URL = "https://api.github.com"
_DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_ALLOWED_API_HOSTS = frozenset({"api.github.com"})
_PER_PAGE = 100
_MAX_PAGES = 10


class GitHubSearchError(RuntimeError):
    """Safe public error for a ranked GitHub Search failure."""


class InvalidGitHubSearchResponse(GitHubSearchError):
    """GitHub returned malformed, incomplete, or policy-ineligible Search data."""


class UnsafeGitHubSearchUrl(GitHubSearchError):
    """A configured Search URL escaped the GitHub API allowlist."""


class _GitHubOwner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    login: str = Field(
        strict=True,
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    id: int = Field(strict=True, gt=0)


class _GitHubLicense(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    spdx_id: str | None = Field(default=None, strict=True, min_length=1)
    name: str | None = Field(default=None, strict=True, min_length=1, max_length=500)


class _GitHubSearchItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int = Field(strict=True, gt=0)
    name: str = Field(strict=True, min_length=1, max_length=100)
    full_name: str = Field(strict=True, min_length=3, max_length=140)
    owner: _GitHubOwner
    html_url: HttpUrl
    description: str | None = Field(default=None, max_length=10_000)
    topics: tuple[str, ...] = ()
    language: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    pushed_at: AwareDatetime | None = None
    stargazers_count: int = Field(strict=True, ge=0)
    archived: StrictBool
    disabled: StrictBool | None = None
    fork: StrictBool
    private: StrictBool
    license: _GitHubLicense | None = None


class _GitHubSearchEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    total_count: int = Field(strict=True, ge=0)
    incomplete_results: StrictBool
    items: tuple[_GitHubSearchItem, ...] = Field(max_length=_PER_PAGE)


@dataclass(frozen=True, slots=True)
class ParsedGitHubSearchPage:
    """One exact Search response re-derived into validated repository facts."""

    raw_bytes: bytes
    raw_sha256: str
    total_count: int
    observations: tuple[RepositoryObservation, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class RankedRepositorySnapshot:
    """One complete, immutable ranked Search window."""

    criteria: CatalogSelectionCriteria
    observations: tuple[RepositoryObservation, ...]
    repository_ranks: tuple[tuple[int, int], ...]
    api_total_count: int
    pages_fetched: int
    result_limit: int
    raw_page_hashes: tuple[str, ...]
    observed_at: datetime


def build_github_search_query(criteria: CatalogSelectionCriteria) -> str:
    """Return the documented repository Search query for one immutable policy."""

    if not isinstance(criteria, CatalogSelectionCriteria):
        raise TypeError("criteria must be CatalogSelectionCriteria")
    cutoff = criteria.pushed_since.astimezone(UTC).date().isoformat()
    return f"stars:>={criteria.min_stars} pushed:>={cutoff} archived:false is:public"


def parse_github_search_page(
    raw_bytes: bytes,
    *,
    observed_at: datetime,
    criteria: CatalogSelectionCriteria,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
) -> ParsedGitHubSearchPage:
    """Parse and locally revalidate one bounded GitHub Search response."""

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    _validate_max_response_bytes(max_response_bytes)
    if len(raw_bytes) > max_response_bytes:
        raise InvalidGitHubSearchResponse("GitHub Search response exceeds byte limit")
    if not isinstance(criteria, CatalogSelectionCriteria):
        raise TypeError("criteria must be CatalogSelectionCriteria")
    normalized_observed_at = _utc_datetime(observed_at, name="observed_at")
    try:
        envelope = _GitHubSearchEnvelope.model_validate_json(raw_bytes)
        if envelope.incomplete_results:
            raise InvalidGitHubSearchResponse("GitHub Search returned incomplete results")
        observations = tuple(
            _observation_from_item(item, observed_at=normalized_observed_at)
            for item in envelope.items
        )
        for observation in observations:
            _validate_selection(observation, criteria)
    except InvalidGitHubSearchResponse:
        raise
    except (TypeError, ValueError, ValidationError):
        raise InvalidGitHubSearchResponse("invalid GitHub Search response") from None
    return ParsedGitHubSearchPage(
        raw_bytes=raw_bytes,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        total_count=envelope.total_count,
        observations=observations,
        observed_at=normalized_observed_at,
    )


class GitHubSearchSource:
    """Fetch a complete page-ranked GitHub Search window in one run."""

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
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        _validate_max_response_bytes(max_response_bytes)
        self._base_url = _validate_base_url(base_url)
        self._token = _validate_token(token)
        self._owns_client = client is None
        self._client = client or httpx.Client(transport=transport, timeout=float(timeout))
        self._timeout = float(timeout)
        self._max_response_bytes = max_response_bytes
        self._now = now or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        """Describe safe configuration without authorization material."""

        return (
            f"{type(self).__name__}(base_url={str(self._base_url)!r}, "
            f"authenticated={self._token is not None}, "
            f"max_response_bytes={self._max_response_bytes})"
        )

    def __enter__(self) -> GitHubSearchSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close only a client owned by this adapter."""

        if self._owns_client:
            self._client.close()

    def collect_snapshot(
        self,
        criteria: CatalogSelectionCriteria,
        *,
        max_pages: int,
        raw_store: RawObjectStore,
    ) -> RankedRepositorySnapshot:
        """Fetch all required pages or raise without returning partial state."""

        if not isinstance(criteria, CatalogSelectionCriteria):
            raise TypeError("criteria must be CatalogSelectionCriteria")
        _validate_max_pages(max_pages)
        if criteria.result_limit > max_pages * _PER_PAGE:
            raise ValueError("criteria result_limit exceeds max_pages capacity")
        if not isinstance(raw_store, RawObjectStore):
            raise TypeError("raw_store must be RawObjectStore")

        raw_page_hashes: list[str] = []
        raw_pages: list[bytes] = []
        page_observed_at: list[datetime] = []
        api_total_count: int | None = None
        required_pages = 1

        for page_number in range(1, max_pages + 1):
            raw_bytes = self._request_page(criteria, page_number)
            observed_after_response = _utc_datetime(self._now(), name="run clock")
            raw_object = raw_store.write(raw_bytes)
            parsed = parse_github_search_page(
                raw_bytes,
                observed_at=observed_after_response,
                criteria=criteria,
                max_response_bytes=self._max_response_bytes,
            )
            raw_page_hashes.append(raw_object.sha256)
            raw_pages.append(raw_bytes)
            page_observed_at.append(observed_after_response)
            if api_total_count is None:
                api_total_count = parsed.total_count
                target_count = min(api_total_count, criteria.result_limit)
                required_pages = max(1, ceil(target_count / _PER_PAGE))
            elif parsed.total_count != api_total_count:
                raise InvalidGitHubSearchResponse("GitHub Search total_count changed during run")

            expected_items = min(
                _PER_PAGE,
                max(0, api_total_count - ((page_number - 1) * _PER_PAGE)),
            )
            if len(parsed.observations) != expected_items:
                raise InvalidGitHubSearchResponse("GitHub Search returned an unexpected short page")
            if page_number == required_pages:
                break
        else:  # pragma: no cover - guarded by the result_limit capacity check
            raise InvalidGitHubSearchResponse("GitHub Search page budget ended early")

        if api_total_count is None:  # pragma: no cover - page one is always requested
            raise InvalidGitHubSearchResponse("GitHub Search returned no page")
        observed_at = max(page_observed_at)
        collected = tuple(
            observation
            for raw_bytes in raw_pages
            for observation in parse_github_search_page(
                raw_bytes,
                observed_at=observed_at,
                criteria=criteria,
                max_response_bytes=self._max_response_bytes,
            ).observations
        )
        ranked = _rank_observations(collected, result_limit=criteria.result_limit)
        target_count = min(api_total_count, criteria.result_limit)
        if len(ranked) != target_count:
            raise InvalidGitHubSearchResponse(
                "GitHub Search did not provide enough unique repositories"
            )
        ranks = tuple(
            (observation.identity.repository_id, rank)
            for rank, observation in enumerate(ranked, start=1)
        )
        return RankedRepositorySnapshot(
            criteria=criteria,
            observations=ranked,
            repository_ranks=ranks,
            api_total_count=api_total_count,
            pages_fetched=len(raw_page_hashes),
            result_limit=criteria.result_limit,
            raw_page_hashes=tuple(raw_page_hashes),
            observed_at=observed_at,
        )

    def _request_page(self, criteria: CatalogSelectionCriteria, page_number: int) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "github-module-catalog/0.1",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request_url = self._base_url.copy_with(path="/search/repositories")
        try:
            with self._client.stream(
                "GET",
                request_url,
                params={
                    "q": build_github_search_query(criteria),
                    "sort": criteria.sort,
                    "order": criteria.order,
                    "per_page": str(_PER_PAGE),
                    "page": str(page_number),
                },
                headers=headers,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    raise GitHubSearchError(
                        f"GitHub Search returned unexpected status {response.status_code}"
                    )
                return _read_bounded_body(response, self._max_response_bytes)
        except GitHubSearchError:
            raise
        except httpx.HTTPError:
            raise GitHubSearchError("GitHub Search request failed") from None


def _observation_from_item(
    item: _GitHubSearchItem, *, observed_at: datetime
) -> RepositoryObservation:
    license_spdx = item.license.spdx_id if item.license is not None else None
    license_name = item.license.name if item.license is not None else None
    return RepositoryObservation(
        identity=RepositoryIdentity(repository_id=item.id),
        owner=item.owner.login,
        name=item.name,
        full_name=item.full_name,
        html_url=item.html_url,
        description=item.description,
        topics=item.topics,
        primary_language=item.language,
        created_at=item.created_at,
        updated_at=item.updated_at,
        pushed_at=item.pushed_at,
        stargazers_count=item.stargazers_count,
        observed_at=observed_at,
        archived=item.archived,
        disabled=item.disabled,
        fork=item.fork,
        private=item.private,
        license_spdx=license_spdx,
        license_name=license_name,
    )


def _validate_selection(
    observation: RepositoryObservation, criteria: CatalogSelectionCriteria
) -> None:
    if (
        observation.stargazers_count is None
        or observation.stargazers_count < criteria.min_stars
        or observation.pushed_at is None
        or observation.pushed_at < criteria.pushed_since
        or observation.archived is not False
        or observation.fork is not False
        or observation.private is not False
    ):
        raise InvalidGitHubSearchResponse(
            "GitHub Search repository does not satisfy the selection policy"
        )


def _rank_observations(
    observations: tuple[RepositoryObservation, ...], *, result_limit: int
) -> tuple[RepositoryObservation, ...]:
    by_repository_id: dict[int, RepositoryObservation] = {}
    for observation in observations:
        repository_id = observation.identity.repository_id
        existing = by_repository_id.get(repository_id)
        if existing is not None and existing != observation:
            raise InvalidGitHubSearchResponse(
                "GitHub Search returned conflicting duplicate repository facts"
            )
        by_repository_id.setdefault(repository_id, observation)
    return tuple(sorted(by_repository_id.values(), key=_rank_key)[:result_limit])


def _rank_key(observation: RepositoryObservation) -> tuple[int, int]:
    stars = observation.stargazers_count
    if stars is None:  # pragma: no cover - selection validation guarantees this fact
        raise InvalidGitHubSearchResponse("ranked repository has no star count")
    return (-stars, observation.identity.repository_id)


def _read_bounded_body(response: httpx.Response, max_response_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise InvalidGitHubSearchResponse("invalid GitHub Content-Length header") from None
        if declared_length < 0:
            raise InvalidGitHubSearchResponse("invalid GitHub Content-Length header")
        if declared_length > max_response_bytes:
            raise InvalidGitHubSearchResponse("GitHub Search response exceeds byte limit")
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(chunk) > max_response_bytes - len(body):
            raise InvalidGitHubSearchResponse("GitHub Search response exceeds byte limit")
        body.extend(chunk)
    return bytes(body)


def _validate_max_response_bytes(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("max_response_bytes must be a positive integer")


def _validate_max_pages(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_PAGES:
        raise ValueError("max_pages must be an integer from 1 through 10")


def _validate_token(token: str | None) -> str | None:
    if token is None:
        return None
    if (
        not token
        or len(token) > 1_024
        or token != token.strip()
        or any(ord(character) < 32 for character in token)
    ):
        raise ValueError("invalid GitHub token")
    return token


def _validate_base_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (TypeError, ValueError):
        raise UnsafeGitHubSearchUrl("GitHub Search API URL is not allowed") from None
    if (
        url.scheme != "https"
        or url.host not in _ALLOWED_API_HOSTS
        or url.port not in {None, 443}
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise UnsafeGitHubSearchUrl("GitHub Search API URL is not allowed")
    return url.copy_with(path="/")


def _utc_datetime(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
