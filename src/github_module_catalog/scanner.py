"""Bounded, resumable discovery orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from github_module_catalog.source import (
    PageResult,
    RepositoryPage,
    RepositorySource,
    RetryDecision,
    RetryResult,
    UnchangedResult,
)
from github_module_catalog.state import DiscoveryPageRecord, StateStore
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
    observations_recorded: int = 0
    observation_failures: int = 0
    retry_decision: RetryDecision | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class _ScanProgress:
    run_id: int
    cursor_start: int
    cursor: int
    pages_committed: int = 0
    observations_recorded: int = 0
    observation_failures: int = 0


@dataclass(frozen=True, slots=True)
class _CommittedPage:
    result: PageResult
    record: DiscoveryPageRecord


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
        progress = _ScanProgress(run.id, run.discovery_cursor, run.discovery_cursor)
        for _ in range(max_pages):
            fetched = self._fetch_and_commit(progress, committed_at=started_at)
            if isinstance(fetched, ScanOutcome):
                return fetched
            recorded, failed = self._record_observations(
                fetched.result.page,
                fetched.record,
                occurred_at=started_at,
            )
            progress = _ScanProgress(
                run_id=progress.run_id,
                cursor_start=progress.cursor_start,
                cursor=fetched.record.cursor_after,
                pages_committed=progress.pages_committed + int(fetched.record.newly_committed),
                observations_recorded=progress.observations_recorded + recorded,
                observation_failures=progress.observation_failures + failed,
            )
            if (
                fetched.result.page.next_url is None
                or progress.cursor <= fetched.record.cursor_before
            ):
                return self._outcome(ScanStatus.COMPLETED, progress)
        return self._outcome(ScanStatus.PAGE_LIMIT_REACHED, progress)

    def _fetch_and_commit(
        self, progress: _ScanProgress, *, committed_at: datetime
    ) -> _CommittedPage | ScanOutcome:
        try:
            result = self._source.fetch_page(progress.cursor)
            if isinstance(result, RetryResult):
                return self._outcome(ScanStatus.RETRY, progress, result.decision)
            if isinstance(result, UnchangedResult):
                return self._outcome(ScanStatus.UNCHANGED, progress)
            if not isinstance(result, PageResult):
                raise TypeError("repository source returned an unsupported result")
            self._raw_store.write(
                result.page.raw_bytes,
                expected_sha256=result.page.raw_sha256,
            )
            record = self._state.commit_discovery_page(
                progress.run_id,
                cursor_before=progress.cursor,
                page=result.page,
                committed_at=committed_at,
            )
            return _CommittedPage(result, record)
        except Exception as error:
            durable = self._state.get_discovery_cursor(progress.run_id)
            return self._outcome(
                ScanStatus.ERROR,
                _ScanProgress(
                    progress.run_id,
                    progress.cursor_start,
                    durable,
                    progress.pages_committed,
                    progress.observations_recorded,
                    progress.observation_failures,
                ),
                error_type=type(error).__name__,
            )

    def _record_observations(
        self,
        page: RepositoryPage,
        record: DiscoveryPageRecord,
        *,
        occurred_at: datetime,
    ) -> tuple[int, int]:
        recorded = 0
        failed = 0
        for observation in page.observations:
            try:
                if observation.detail_metadata_complete:
                    self._state.complete_discovery_observation(
                        record.id,
                        page.raw_sha256,
                        observation,
                        occurred_at=occurred_at,
                    )
                else:
                    self._state.record_discovery_observation(
                        record.id, page.raw_sha256, observation
                    )
            except Exception as error:
                failed += 1
                self._try_append_observation_event(
                    observation.identity.repository_id,
                    page,
                    "retry",
                    occurred_at,
                    error_type=type(error).__name__,
                )
            else:
                recorded += 1
        return recorded, failed

    def _try_append_observation_event(
        self,
        repository_id: int,
        page: RepositoryPage,
        event: str,
        occurred_at: datetime,
        *,
        error_type: str | None = None,
    ) -> bool:
        details = None if error_type is None else {"error_type": error_type}
        try:
            self._state.append_work_item_event(
                repository_id,
                "enrichment",
                event,
                occurred_at=occurred_at,
                source_revision=page.raw_sha256,
                analyzer_version="inventory-v1",
                details=details,
            )
        except Exception:
            return False
        return True

    @staticmethod
    def _outcome(
        status: ScanStatus,
        progress: _ScanProgress,
        retry_decision: RetryDecision | None = None,
        *,
        error_type: str | None = None,
    ) -> ScanOutcome:
        return ScanOutcome(
            status=status,
            crawl_run_id=progress.run_id,
            cursor_start=progress.cursor_start,
            cursor_end=progress.cursor,
            pages_committed=progress.pages_committed,
            observations_recorded=progress.observations_recorded,
            observation_failures=progress.observation_failures,
            retry_decision=retry_decision,
            error_type=error_type,
        )
