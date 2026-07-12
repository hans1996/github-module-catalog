"""Crash-safe SQLite state for resumable catalog processing."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from github_module_catalog.models import RepositoryObservation
from github_module_catalog.source import RepositoryInventoryIdentity, RepositoryPage
from github_module_catalog.storage import RawObjectStore

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api-key",
        "api_key",
        "authorization",
        "client_secret",
        "github_token",
        "password",
        "proxy-authorization",
        "token",
        "x-api-key",
    }
)
_NORMALIZED_SENSITIVE_KEYS = frozenset(key.replace("-", "_") for key in _SENSITIVE_KEYS)


class StateConflictError(RuntimeError):
    """Raised when a caller tries to advance a stale or conflicting cursor."""


class SensitiveStateError(ValueError):
    """Raised before credential-bearing material can enter durable state."""


@dataclass(frozen=True, slots=True)
class CrawlRunRecord:
    """One discovery run and its durable cursor."""

    id: int
    source: str
    started_at: datetime
    discovery_cursor: int


@dataclass(frozen=True, slots=True)
class DiscoveryPageRecord:
    """Metadata for a raw page committed to a crawl run."""

    id: int
    crawl_run_id: int
    cursor_before: int
    cursor_after: int
    raw_sha256: str
    raw_size_bytes: int
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class RepositoryIdentityRecord:
    """A stable numeric repository identity with its latest explicit name facts."""

    repository_id: int
    owner_login: str
    name: str
    full_name: str
    owner_id: int | None
    html_url: str


@dataclass(frozen=True, slots=True)
class RepositoryObservationRecord:
    """An immutable validated observation snapshot."""

    repository_id: int
    observation_hash: str
    observation_json: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WorkItemEventRecord:
    """One append-only processing event."""

    id: int
    repository_id: int
    stage: str
    event: str
    occurred_at: datetime
    source_revision: str | None
    analyzer_version: str | None
    details_json: str | None


@dataclass(frozen=True, slots=True)
class WorkItemRecord:
    """One uniquely keyed unit of processing work."""

    repository_id: int
    stage: str
    source_revision: str
    analyzer_version: str
    queued_at: datetime


@dataclass(frozen=True, slots=True)
class StageCheckpointRecord:
    """An independently addressable stage checkpoint."""

    stage: str
    partition_key: str
    value: str
    updated_at: datetime


class StateStore:
    """SQLite repository whose write methods use explicit transactions."""

    def __init__(self, path: Path, raw_store: RawObjectStore) -> None:
        self._path = Path(path).resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_store = raw_store
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        journal_row = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
        self._journal_mode = str(journal_row[0]).casefold()
        self._initialize_schema()

    @property
    def path(self) -> Path:
        """Return the configured database path."""

        return self._path

    @property
    def foreign_keys_enabled(self) -> bool:
        """Report the connection's enforced foreign-key mode."""

        row = self._connection.execute("PRAGMA foreign_keys").fetchone()
        return bool(row[0])

    @property
    def journal_mode(self) -> str:
        """Return the journal mode SQLite accepted for this database."""

        return self._journal_mode

    def close(self) -> None:
        """Close the owned SQLite connection."""

        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_crawl_run(
        self,
        source: str,
        *,
        started_at: datetime,
        initial_cursor: int | None = None,
    ) -> CrawlRunRecord:
        """Create a run that resumes from the most recently durable cursor."""

        source = _validated_text(source, "source")
        started_text = _datetime_text(started_at)
        with self._transaction() as connection:
            cursor = initial_cursor
            if cursor is None:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(discovery_cursor), 0)
                    FROM crawl_runs WHERE source = ?
                    """,
                    (source,),
                ).fetchone()
                cursor = int(row[0])
            if cursor < 0:
                raise ValueError("initial_cursor must be nonnegative")
            result = connection.execute(
                "INSERT INTO crawl_runs(source, started_at, discovery_cursor) VALUES (?, ?, ?)",
                (source, started_text, cursor),
            )
            run_id = _last_row_id(result)
        return CrawlRunRecord(run_id, source, _parse_datetime(started_text), cursor)

    def get_discovery_cursor(self, crawl_run_id: int) -> int:
        """Return one run's durable discovery cursor."""

        row = self._connection.execute(
            "SELECT discovery_cursor FROM crawl_runs WHERE id = ?", (crawl_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown crawl run: {crawl_run_id}")
        return int(row[0])

    def commit_discovery_page(
        self,
        crawl_run_id: int,
        *,
        cursor_before: int,
        page: RepositoryPage,
        committed_at: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> DiscoveryPageRecord:
        """Commit page metadata, identities, queued events, and cursor atomically."""

        raw_object = self._raw_store.verify(page.raw_sha256, expected_bytes=page.raw_bytes)
        _validate_repository_page_state(page)
        cursor_after = _cursor_after(cursor_before, page)
        committed_text = _datetime_text(committed_at)
        rate_limit_json = json.dumps(
            {
                "limit": page.rate_limit.limit,
                "remaining": page.rate_limit.remaining,
                "reset_epoch": page.rate_limit.reset_epoch,
                "resource": page.rate_limit.resource,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, crawl_run_id, cursor_before, cursor_after, raw_sha256,
                       raw_size_bytes, committed_at
                FROM discovery_pages
                WHERE crawl_run_id = ? AND cursor_before = ?
                """,
                (crawl_run_id, cursor_before),
            ).fetchone()
            if existing is not None:
                if existing["raw_sha256"] != page.raw_sha256:
                    raise StateConflictError("cursor already committed with different raw bytes")
                return _page_record(existing)

            cursor_row = connection.execute(
                "SELECT discovery_cursor FROM crawl_runs WHERE id = ?", (crawl_run_id,)
            ).fetchone()
            if cursor_row is None:
                raise KeyError(f"unknown crawl run: {crawl_run_id}")
            if int(cursor_row[0]) != cursor_before:
                raise StateConflictError("cursor_before does not match the durable cursor")

            result = connection.execute(
                """
                INSERT INTO discovery_pages(
                    crawl_run_id, cursor_before, cursor_after, raw_sha256,
                    raw_size_bytes, etag, next_url, rate_limit_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    crawl_run_id,
                    cursor_before,
                    cursor_after,
                    page.raw_sha256,
                    raw_object.size_bytes,
                    page.etag,
                    page.next_url,
                    rate_limit_json,
                    committed_text,
                ),
            )
            page_id = _last_row_id(result)
            for identity in page.identities:
                if self._insert_discovered_identity(connection, identity, committed_text):
                    self._insert_work_item(
                        connection,
                        identity.repository_id,
                        "enrichment",
                        page.raw_sha256,
                        "inventory-v1",
                        committed_text,
                    )
            updated = connection.execute(
                """
                UPDATE crawl_runs SET discovery_cursor = ?
                WHERE id = ? AND discovery_cursor = ?
                """,
                (cursor_after, crawl_run_id, cursor_before),
            )
            if updated.rowcount != 1:
                raise StateConflictError("durable cursor changed during page commit")
            if before_commit is not None:
                before_commit()

        return DiscoveryPageRecord(
            page_id,
            crawl_run_id,
            cursor_before,
            cursor_after,
            page.raw_sha256,
            raw_object.size_bytes,
            _parse_datetime(committed_text),
        )

    def list_discovery_pages(self, crawl_run_id: int) -> tuple[DiscoveryPageRecord, ...]:
        """Return committed pages in cursor order."""

        rows = self._connection.execute(
            """
            SELECT id, crawl_run_id, cursor_before, cursor_after, raw_sha256,
                   raw_size_bytes, committed_at
            FROM discovery_pages WHERE crawl_run_id = ? ORDER BY cursor_before, id
            """,
            (crawl_run_id,),
        ).fetchall()
        return tuple(_page_record(row) for row in rows)

    def list_repository_identities(self) -> tuple[RepositoryIdentityRecord, ...]:
        """Return stable identities ordered by numeric GitHub ID."""

        rows = self._connection.execute(
            """
            SELECT repository_id, owner_login, name, full_name, owner_id, html_url
            FROM repository_identities ORDER BY repository_id
            """
        ).fetchall()
        return tuple(
            RepositoryIdentityRecord(
                repository_id=int(row["repository_id"]),
                owner_login=str(row["owner_login"]),
                name=str(row["name"]),
                full_name=str(row["full_name"]),
                owner_id=None if row["owner_id"] is None else int(row["owner_id"]),
                html_url=str(row["html_url"]),
            )
            for row in rows
        )

    def record_repository_observation(
        self, observation: RepositoryObservation
    ) -> RepositoryObservationRecord:
        """Persist validated facts and explicitly update mutable identity names."""

        observation_json = observation.stable_json()
        _reject_credential_text(observation_json)
        observation_hash = observation.stable_hash()
        observed_text = _datetime_text(observation.observed_at)
        repository_id = observation.identity.repository_id
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO repository_identities(
                    repository_id, owner_login, name, full_name, owner_id, html_url,
                    first_seen_at, last_observed_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    owner_login = excluded.owner_login,
                    name = excluded.name,
                    full_name = excluded.full_name,
                    html_url = excluded.html_url,
                    last_observed_at = excluded.last_observed_at
                """,
                (
                    repository_id,
                    observation.owner,
                    observation.name,
                    observation.full_name,
                    str(observation.html_url),
                    observed_text,
                    observed_text,
                ),
            )
            connection.execute(
                """
                INSERT INTO repository_observations(
                    repository_id, observation_hash, observation_json, observed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(repository_id, observation_hash) DO NOTHING
                """,
                (repository_id, observation_hash, observation_json, observed_text),
            )
        return RepositoryObservationRecord(
            repository_id, observation_hash, observation_json, _parse_datetime(observed_text)
        )

    def list_repository_observations(
        self, repository_id: int
    ) -> tuple[RepositoryObservationRecord, ...]:
        """Return immutable validated observations in insertion order."""

        rows = self._connection.execute(
            """
            SELECT repository_id, observation_hash, observation_json, observed_at
            FROM repository_observations WHERE repository_id = ? ORDER BY id
            """,
            (repository_id,),
        ).fetchall()
        return tuple(
            RepositoryObservationRecord(
                int(row["repository_id"]),
                str(row["observation_hash"]),
                str(row["observation_json"]),
                _parse_datetime(str(row["observed_at"])),
            )
            for row in rows
        )

    def list_latest_repository_observations(self) -> tuple[RepositoryObservation, ...]:
        """Return only hash-verified, Pydantic-validated latest observations."""

        rows = self._connection.execute(
            """
            SELECT observed.repository_id, observed.observation_hash,
                   observed.observation_json
            FROM repository_observations AS observed
            WHERE observed.id = (
                SELECT MAX(candidate.id) FROM repository_observations AS candidate
                WHERE candidate.repository_id = observed.repository_id
            )
            ORDER BY observed.repository_id
            """
        ).fetchall()
        observations: list[RepositoryObservation] = []
        for row in rows:
            observation = RepositoryObservation.model_validate_json(str(row["observation_json"]))
            if observation.identity.repository_id != int(row["repository_id"]):
                raise StateConflictError("observation identity does not match state key")
            if observation.stable_hash() != str(row["observation_hash"]):
                raise StateConflictError("observation hash does not match validated content")
            observations.append(observation)
        return tuple(observations)

    def append_work_item_event(
        self,
        repository_id: int,
        stage: str,
        event: str,
        *,
        occurred_at: datetime,
        source_revision: str | None = None,
        analyzer_version: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> WorkItemEventRecord:
        """Append an immutable processing event."""

        stage = _validated_text(stage, "stage")
        event = _validated_text(event, "event")
        if source_revision is not None:
            _reject_credential_text(source_revision)
        if analyzer_version is not None:
            _reject_credential_text(analyzer_version)
        details_json = _safe_details_json(details)
        occurred_text = _datetime_text(occurred_at)
        with self._transaction() as connection:
            result = connection.execute(
                """
                INSERT INTO work_item_events(
                    repository_id, stage, event, occurred_at, source_revision,
                    analyzer_version, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    stage,
                    event,
                    occurred_text,
                    source_revision,
                    analyzer_version,
                    details_json,
                ),
            )
            event_id = _last_row_id(result)
        return WorkItemEventRecord(
            event_id,
            repository_id,
            stage,
            event,
            _parse_datetime(occurred_text),
            source_revision,
            analyzer_version,
            details_json,
        )

    def queue_work_item(
        self,
        repository_id: int,
        stage: str,
        source_revision: str,
        analyzer_version: str,
        *,
        queued_at: datetime,
    ) -> bool:
        """Queue a unique work key and append an event only on first insertion."""

        stage = _validated_text(stage, "stage")
        source_revision = _validated_text(source_revision, "source_revision")
        analyzer_version = _validated_text(analyzer_version, "analyzer_version")
        queued_text = _datetime_text(queued_at)
        with self._transaction() as connection:
            return self._insert_work_item(
                connection,
                repository_id,
                stage,
                source_revision,
                analyzer_version,
                queued_text,
            )

    def list_work_items(self, *, stage: str | None = None) -> tuple[WorkItemRecord, ...]:
        """Return unique work keys in deterministic order."""

        rows = self._connection.execute(
            """
            SELECT repository_id, stage, source_revision, analyzer_version, queued_at
            FROM work_items WHERE (? IS NULL OR stage = ?)
            ORDER BY repository_id, stage, source_revision, analyzer_version
            """,
            (stage, stage),
        ).fetchall()
        return tuple(
            WorkItemRecord(
                int(row["repository_id"]),
                str(row["stage"]),
                str(row["source_revision"]),
                str(row["analyzer_version"]),
                _parse_datetime(str(row["queued_at"])),
            )
            for row in rows
        )

    def list_work_item_events(
        self, *, repository_id: int | None = None, stage: str | None = None
    ) -> tuple[WorkItemEventRecord, ...]:
        """Return append-only event history in insertion order."""

        rows = self._connection.execute(
            """
            SELECT id, repository_id, stage, event, occurred_at, source_revision,
                   analyzer_version, details_json
            FROM work_item_events
            WHERE (? IS NULL OR repository_id = ?)
              AND (? IS NULL OR stage = ?)
            ORDER BY id
            """,
            (repository_id, repository_id, stage, stage),
        ).fetchall()
        return tuple(_event_record(row) for row in rows)

    def set_stage_checkpoint(
        self,
        stage: str,
        value: str,
        *,
        updated_at: datetime,
        partition_key: str = "global",
    ) -> StageCheckpointRecord:
        """Set one stage/partition checkpoint without affecting other stages."""

        stage = _validated_text(stage, "stage")
        partition_key = _validated_text(partition_key, "partition_key")
        value = _validated_text(value, "value")
        updated_text = _datetime_text(updated_at)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO stage_checkpoints(stage, partition_key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stage, partition_key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (stage, partition_key, value, updated_text),
            )
        return StageCheckpointRecord(stage, partition_key, value, _parse_datetime(updated_text))

    def get_stage_checkpoint(
        self, stage: str, *, partition_key: str = "global"
    ) -> StageCheckpointRecord | None:
        """Return one checkpoint without exposing connection-owned state."""

        row = self._connection.execute(
            """
            SELECT stage, partition_key, value, updated_at FROM stage_checkpoints
            WHERE stage = ? AND partition_key = ?
            """,
            (stage, partition_key),
        ).fetchone()
        if row is None:
            return None
        return StageCheckpointRecord(
            str(row["stage"]),
            str(row["partition_key"]),
            str(row["value"]),
            _parse_datetime(str(row["updated_at"])),
        )

    def _insert_discovered_identity(
        self,
        connection: sqlite3.Connection,
        identity: RepositoryInventoryIdentity,
        observed_at: str,
    ) -> bool:
        if identity.repository_id <= 0 or identity.owner_id <= 0:
            raise ValueError("repository and owner IDs must be positive")
        result = connection.execute(
            """
            INSERT INTO repository_identities(
                repository_id, owner_login, name, full_name, owner_id, html_url,
                first_seen_at, last_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repository_id) DO NOTHING
            """,
            (
                identity.repository_id,
                identity.owner_login,
                identity.name,
                identity.full_name,
                identity.owner_id,
                identity.html_url,
                observed_at,
                observed_at,
            ),
        )
        return result.rowcount == 1

    @staticmethod
    def _insert_work_item(
        connection: sqlite3.Connection,
        repository_id: int,
        stage: str,
        source_revision: str,
        analyzer_version: str,
        queued_at: str,
    ) -> bool:
        result = connection.execute(
            """
            INSERT INTO work_items(
                repository_id, stage, source_revision, analyzer_version, queued_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(repository_id, stage, source_revision, analyzer_version) DO NOTHING
            """,
            (repository_id, stage, source_revision, analyzer_version, queued_at),
        )
        if result.rowcount != 1:
            return False
        connection.execute(
            """
            INSERT INTO work_item_events(
                repository_id, stage, event, occurred_at, source_revision, analyzer_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (repository_id, stage, "queued", queued_at, source_revision, analyzer_version),
        )
        return True

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        self._connection.executescript(_SCHEMA)


def _cursor_after(cursor_before: int, page: RepositoryPage) -> int:
    if cursor_before < 0:
        raise ValueError("cursor_before must be nonnegative")
    identity_cursor = max(
        (identity.repository_id for identity in page.identities), default=cursor_before
    )
    cursor_after = page.next_cursor if page.next_cursor is not None else identity_cursor
    if cursor_after < cursor_before:
        raise ValueError("page cursor cannot move backwards")
    return cursor_after


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an inserted row ID")
    return cursor.lastrowid


def _page_record(row: sqlite3.Row) -> DiscoveryPageRecord:
    return DiscoveryPageRecord(
        int(row["id"]),
        int(row["crawl_run_id"]),
        int(row["cursor_before"]),
        int(row["cursor_after"]),
        str(row["raw_sha256"]),
        int(row["raw_size_bytes"]),
        _parse_datetime(str(row["committed_at"])),
    )


def _event_record(row: sqlite3.Row) -> WorkItemEventRecord:
    return WorkItemEventRecord(
        int(row["id"]),
        int(row["repository_id"]),
        str(row["stage"]),
        str(row["event"]),
        _parse_datetime(str(row["occurred_at"])),
        None if row["source_revision"] is None else str(row["source_revision"]),
        None if row["analyzer_version"] is None else str(row["analyzer_version"]),
        None if row["details_json"] is None else str(row["details_json"]),
    )


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validated_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    _reject_credential_text(value)
    return value


def _validate_state_url(url: str | None) -> None:
    if url is None:
        return
    _reject_credential_text(url)
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise SensitiveStateError("credential-bearing URLs cannot be stored")
    query_keys = {key.casefold().replace("-", "_") for key, _ in parse_qsl(parsed.query)}
    if query_keys & _NORMALIZED_SENSITIVE_KEYS:
        raise SensitiveStateError("credential-bearing URL query cannot be stored")


def _safe_details_json(details: Mapping[str, object] | None) -> str | None:
    if details is None:
        return None
    _reject_sensitive_keys(details)
    try:
        return json.dumps(details, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("event details must be JSON serializable") from error


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("event detail keys must be strings")
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in _NORMALIZED_SENSITIVE_KEYS:
                raise SensitiveStateError("credential fields cannot be stored in state")
            _reject_sensitive_keys(child)
    elif isinstance(value, str):
        _reject_credential_text(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_sensitive_keys(child)


_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:access[\s_-]*token|api[\s_-]*key|x[\s_-]*api[\s_-]*key|"
    r"github[\s_-]*token|client[\s_-]*secret|password|authorization|"
    r"proxy[\s_-]*authorization|token)\s*[:=]\s*(?=\S)|"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{6,}|"
    r"\bgh[pousr]_[A-Za-z0-9_]{8,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{8,}|"
    r"https?://[^\s/:@]+:[^\s/@]+@"
)


def _reject_credential_text(value: str) -> None:
    if _CREDENTIAL_PATTERN.search(value) is not None:
        raise SensitiveStateError("credential material cannot be stored in state")


def _validate_repository_page_state(page: RepositoryPage) -> None:
    _validate_state_url(page.next_url)
    for value in (page.etag, page.rate_limit.resource):
        if value is not None:
            _reject_credential_text(value)
    for identity in page.identities:
        for value in (
            identity.owner_login,
            identity.name,
            identity.full_name,
            identity.html_url,
        ):
            _reject_credential_text(value)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    discovery_cursor INTEGER NOT NULL CHECK(discovery_cursor >= 0)
);

CREATE TABLE IF NOT EXISTS discovery_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    crawl_run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    cursor_before INTEGER NOT NULL CHECK(cursor_before >= 0),
    cursor_after INTEGER NOT NULL CHECK(cursor_after >= cursor_before),
    raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256) = 64),
    raw_size_bytes INTEGER NOT NULL CHECK(raw_size_bytes >= 0),
    etag TEXT,
    next_url TEXT,
    rate_limit_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    UNIQUE(crawl_run_id, cursor_before)
);

CREATE TABLE IF NOT EXISTS repository_identities (
    repository_id INTEGER PRIMARY KEY CHECK(repository_id > 0),
    owner_login TEXT NOT NULL,
    name TEXT NOT NULL,
    full_name TEXT NOT NULL,
    owner_id INTEGER,
    html_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repository_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repository_identities(repository_id),
    observation_hash TEXT NOT NULL CHECK(length(observation_hash) = 64),
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    observed_at TEXT NOT NULL,
    UNIQUE(repository_id, observation_hash)
);

CREATE TABLE IF NOT EXISTS work_item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id INTEGER NOT NULL REFERENCES repository_identities(repository_id),
    stage TEXT NOT NULL,
    event TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source_revision TEXT,
    analyzer_version TEXT,
    details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json))
);

CREATE TABLE IF NOT EXISTS work_items (
    repository_id INTEGER NOT NULL REFERENCES repository_identities(repository_id),
    stage TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    PRIMARY KEY(repository_id, stage, source_revision, analyzer_version)
);

CREATE TRIGGER IF NOT EXISTS work_item_events_no_update
BEFORE UPDATE ON work_item_events
BEGIN
    SELECT RAISE(ABORT, 'work item events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS work_item_events_no_delete
BEFORE DELETE ON work_item_events
BEGIN
    SELECT RAISE(ABORT, 'work item events are append-only');
END;

CREATE TABLE IF NOT EXISTS stage_checkpoints (
    stage TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stage, partition_key)
);

CREATE TABLE IF NOT EXISTS catalog_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    published_at TEXT NOT NULL,
    UNIQUE(manifest_sha256)
);
"""
