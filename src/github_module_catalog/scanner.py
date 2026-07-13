"""Bounded, resumable discovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from github_module_catalog.source import (
    PageResult,
    RepositorySource,
    RetryDecision,
    RetryResult,
    UnchangedResult,
)
from github_module_catalog.state import StateStore
from github_module_catalog.storage import RawObjectStore


class ScanStatus(StrEnum):
    """Reason a bounded scan returned control to its scheduler."""

    PAGE_LIMIT_REACHED = "page_limit_reached"
    COMPLETED = "completed"
    UNCHANGED = "unchanged"
    RETRY = "retry"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ScanOutcome:
    """Immutable facts about one bounded discovery attempt."""

    status: ScanStatus
    crawl_run_id: int
    cursor_start: int
    cursor_end: int
    pages_committed: int
    retry_decision: RetryDecision | None = None
    error_type: str | None = None


class DiscoveryScanner:
    """Compose a repository source with crash-safe raw and state stores."""

    def __init__(
        self,
        *,
        source: RepositorySource,
        raw_store: RawObjectStore,
        state: StateStore,
        source_name: str,
    ) -> None:
        self._source = source
        self._raw_store = raw_store
        self._state = state
        self._source_name = source_name

    def scan(self, *, max_pages: int, started_at: datetime) -> ScanOutcome:
        """Fetch and commit no more than ``max_pages`` sequential responses."""

        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        run = self._state.create_crawl_run(self._source_name, started_at=started_at)
        cursor_start = run.discovery_cursor
        cursor = cursor_start
        committed = 0
        for _ in range(max_pages):
            try:
                outcome = self._source.fetch_page(cursor)
                if isinstance(outcome, RetryResult):
                    return self._outcome(
                        ScanStatus.RETRY, run.id, cursor_start, cursor, committed, outcome.decision
                    )
                if isinstance(outcome, UnchangedResult):
                    return self._outcome(
                        ScanStatus.UNCHANGED, run.id, cursor_start, cursor, committed
                    )
                if not isinstance(outcome, PageResult):
                    raise TypeError("repository source returned an unsupported result")
                self._raw_store.write(
                    outcome.page.raw_bytes,
                    expected_sha256=outcome.page.raw_sha256,
                )
                page = self._state.commit_discovery_page(
                    run.id,
                    cursor_before=cursor,
                    page=outcome.page,
                    committed_at=started_at,
                )
            except Exception as error:  # source/storage/state errors must not advance the cursor
                return self._outcome(
                    ScanStatus.ERROR,
                    run.id,
                    cursor_start,
                    self._state.get_discovery_cursor(run.id),
                    committed,
                    error_type=type(error).__name__,
                )
            previous_cursor = cursor
            cursor = page.cursor_after
            if page.newly_committed:
                committed += 1
            if outcome.page.next_url is None or cursor <= previous_cursor:
                return self._outcome(ScanStatus.COMPLETED, run.id, cursor_start, cursor, committed)
        return self._outcome(ScanStatus.PAGE_LIMIT_REACHED, run.id, cursor_start, cursor, committed)

    @staticmethod
    def _outcome(
        status: ScanStatus,
        run_id: int,
        cursor_start: int,
        cursor_end: int,
        committed: int,
        retry_decision: RetryDecision | None = None,
        *,
        error_type: str | None = None,
    ) -> ScanOutcome:
        return ScanOutcome(
            status=status,
            crawl_run_id=run_id,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            pages_committed=committed,
            retry_decision=retry_decision,
            error_type=error_type,
        )
