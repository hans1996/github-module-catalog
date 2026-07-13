"""Source contracts and immutable discovery outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from github_module_catalog.models import RepositoryObservation


@dataclass(frozen=True, slots=True)
class RepositoryInventoryIdentity:
    """Minimal identity facts emitted by a repository inventory source."""

    repository_id: int
    name: str
    full_name: str
    owner_login: str
    owner_id: int
    html_url: str


@dataclass(frozen=True, slots=True)
class RateLimitFacts:
    """Rate-limit headers reported by the upstream service."""

    limit: int | None = None
    remaining: int | None = None
    reset_epoch: int | None = None
    resource: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryPage:
    """One validated inventory response with its exact source bytes."""

    raw_bytes: bytes
    raw_sha256: str
    etag: str | None
    next_url: str | None
    next_cursor: int | None
    rate_limit: RateLimitFacts
    identities: tuple[RepositoryInventoryIdentity, ...]
    observations: tuple[RepositoryObservation, ...] = ()
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PageResult:
    """A newly fetched repository page."""

    page: RepositoryPage


@dataclass(frozen=True, slots=True)
class UnchangedResult:
    """A conditional request whose representation has not changed."""

    etag: str | None
    rate_limit: RateLimitFacts


class RetrySource(StrEnum):
    """Upstream fact used to schedule a retry."""

    RETRY_AFTER = "retry_after"
    RATE_LIMIT_RESET = "rate_limit_reset"


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """A scheduler-owned retry time; sources never sleep."""

    source: RetrySource
    delay_seconds: int
    retry_at: datetime


@dataclass(frozen=True, slots=True)
class RetryResult:
    """A retryable upstream response."""

    status_code: int
    decision: RetryDecision
    rate_limit: RateLimitFacts


type RepositoryFetchResult = PageResult | UnchangedResult | RetryResult


@runtime_checkable
class RepositorySource(Protocol):
    """Boundary for resumable repository inventory sources."""

    def fetch_page(self, cursor: int, *, etag: str | None = None) -> RepositoryFetchResult:
        """Fetch one page after a durable numeric cursor."""
