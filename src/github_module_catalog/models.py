"""Immutable domain models for repository observations and catalog output."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)

_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,49}$")
_SPDX_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+-]*"
    r"(?:\s+(?:AND|OR|WITH)\s+[A-Za-z0-9][A-Za-z0-9.+-]*)*$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_PERMISSIVE_LICENSES = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MIT",
        "Unlicense",
    }
)


def _validate_nonempty(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("must not be empty")
    return normalized


def _validate_license_spdx(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or not _SPDX_PATTERN.fullmatch(normalized):
        raise ValueError("license_spdx must be an SPDX-shaped identifier or expression")
    return normalized


NonEmptyStr = Annotated[str, AfterValidator(_validate_nonempty)]


class ImmutableModel(BaseModel):
    """Base model with a canonical byte representation."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    def stable_json(self) -> str:
        """Return canonical JSON suitable for hashing and byte comparisons."""

        return json.dumps(
            self._stable_document(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _stable_document(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_computed_fields=True)

    def stable_hash(self) -> str:
        """Return the SHA-256 of the canonical JSON representation."""

        return hashlib.sha256(self.stable_json().encode("utf-8")).hexdigest()


class ReuseStatus(StrEnum):
    """License/lifecycle signal; this is not legal advice."""

    DISCOVERY_ONLY = "discovery_only"
    SAFE_TO_INTEGRATE = "safe_to_integrate"


class RepositoryIdentity(ImmutableModel):
    """Stable GitHub identity, independent of mutable owner and repository names."""

    repository_id: int = Field(gt=0)


class CatalogSelectionCriteria(ImmutableModel):
    """Immutable eligibility and ordering policy for a ranked Search snapshot."""

    min_stars: int = Field(ge=0, strict=True)
    pushed_since: AwareDatetime
    exclude_archived: StrictBool = True
    exclude_forks: StrictBool = True
    public_only: StrictBool = True
    sort: Literal["stars"] = "stars"
    order: Literal["desc"] = "desc"
    result_limit: int = Field(gt=0, le=1_000, strict=True)

    @field_validator("exclude_archived", "exclude_forks", "public_only")
    @classmethod
    def require_fail_closed_flags(cls, value: bool, info: ValidationInfo) -> bool:
        if value is not True:
            raise ValueError(f"{info.field_name} must be true")
        return value

    @field_validator("pushed_since")
    @classmethod
    def require_utc_cutoff(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("pushed_since must use UTC")
        return value.astimezone(UTC)


class CatalogSearchPageEvidence(ImmutableModel):
    """Ordered request identity and raw response binding for one Search page."""

    page_number: int = Field(strict=True, ge=1, le=10)
    query: NonEmptyStr = Field(max_length=500)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RepositoryObservation(ImmutableModel):
    """Validated source facts observed for a public GitHub repository."""

    identity: RepositoryIdentity
    owner: str = Field(
        min_length=1,
        max_length=39,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    )
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(min_length=3, max_length=140)
    html_url: HttpUrl
    description: str | None = Field(default=None, max_length=10_000)
    topics: tuple[str, ...] = ()
    primary_language: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    pushed_at: AwareDatetime | None = None
    stargazers_count: int | None = Field(default=None, ge=0, strict=True)
    observed_at: AwareDatetime
    archived: StrictBool | None = None
    disabled: StrictBool | None = None
    fork: StrictBool | None = None
    private: StrictBool | None = None
    license_spdx: str | None = None
    license_name: str | None = Field(default=None, min_length=1, max_length=500)

    def _stable_document(self) -> dict[str, object]:
        document = super()._stable_document()
        # Unknown facts added for ranked Search remain absent from legacy canonical hashes.
        for field_name in ("stargazers_count", "private"):
            if document[field_name] is None:
                del document[field_name]
        return document

    @field_validator("topics", mode="before")
    @classmethod
    def normalize_topics(cls, value: object) -> tuple[str, ...]:
        """Normalize GitHub topics into a deduplicated canonical tuple."""

        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("topics must be a collection of topic strings")
        normalized_topics: set[str] = set()
        for topic in value:
            if not isinstance(topic, str):
                raise ValueError("topics must contain only strings")
            normalized = topic.strip().casefold()
            if not _TOPIC_PATTERN.fullmatch(normalized):
                raise ValueError(f"invalid GitHub topic: {topic!r}")
            normalized_topics.add(normalized)
        return tuple(sorted(normalized_topics))

    @field_validator("license_spdx")
    @classmethod
    def validate_license_spdx(cls, value: str | None) -> str | None:
        """Accept SPDX-shaped identifiers and expressions without granting reuse rights."""

        return _validate_license_spdx(value)

    @model_validator(mode="after")
    def validate_repository_facts(self) -> Self:
        expected_full_name = f"{self.owner}/{self.name}"
        if self.full_name != expected_full_name:
            raise ValueError(f"full_name must equal {expected_full_name!r}")
        if self.html_url.scheme != "https" or self.html_url.host not in {
            "github.com",
            "www.github.com",
        }:
            raise ValueError("html_url must be an HTTPS github.com URL")
        if (
            self.html_url.username is not None
            or self.html_url.password is not None
            or self.html_url.query is not None
            or self.html_url.fragment is not None
            or self.html_url.port not in {None, 443}
        ):
            raise ValueError("html_url must not contain credentials, suffixes, or custom ports")
        expected_path = f"/{self.owner}/{self.name}"
        observed_path = (self.html_url.path or "").rstrip("/")
        if observed_path != expected_path:
            raise ValueError(f"html_url path must equal {expected_path!r}")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")
        if (
            self.created_at is not None
            and self.pushed_at is not None
            and self.pushed_at < self.created_at
        ):
            raise ValueError("pushed_at cannot precede created_at")
        for field_name, timestamp in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("pushed_at", self.pushed_at),
        ):
            if timestamp is not None and self.observed_at < timestamp:
                raise ValueError(f"observed_at cannot precede {field_name}")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reuse_status(self) -> ReuseStatus:
        """Return a conservative reuse signal, never a legal conclusion."""

        if self.archived is not False or self.disabled is not False:
            return ReuseStatus.DISCOVERY_ONLY
        if self.license_spdx in _PERMISSIVE_LICENSES:
            return ReuseStatus.SAFE_TO_INTEGRATE
        return ReuseStatus.DISCOVERY_ONLY

    @property
    def detail_metadata_complete(self) -> bool:
        """Report whether required detail-endpoint facts are explicitly known."""

        return all(
            value is not None
            for value in (
                self.created_at,
                self.updated_at,
                self.archived,
                self.disabled,
                self.fork,
            )
        )


EvidenceSource = Literal[
    "topic",
    "description",
    "language",
    "lifecycle",
    "license",
    "taxonomy",
]


class Evidence(ImmutableModel):
    """A source fact that supports a classifier assertion."""

    source: EvidenceSource
    value: NonEmptyStr = Field(max_length=1_000)


class CapabilityAssertion(ImmutableModel):
    """Versioned, traceable claim that a repository provides a capability."""

    repository_id: int = Field(gt=0)
    capability_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=_CAPABILITY_ID_PATTERN,
    )
    taxonomy_version: NonEmptyStr = Field(max_length=100)
    classifier_version: NonEmptyStr = Field(max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    source_observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_spdx: str | None = None
    reuse_status: ReuseStatus

    @field_validator("source_observation_hash")
    @classmethod
    def validate_observation_hash(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("source_observation_hash must be a lowercase SHA-256 digest")
        return value

    @field_validator("license_spdx")
    @classmethod
    def validate_license_spdx(cls, value: str | None) -> str | None:
        return _validate_license_spdx(value)

    @field_validator("evidence")
    @classmethod
    def canonicalize_evidence(cls, value: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        return tuple(sorted(set(value), key=lambda item: (item.source, item.value)))


class CapabilityDefinition(ImmutableModel):
    """Published capability identity and its direct taxonomy parents."""

    id: str = Field(min_length=1, max_length=100, pattern=_CAPABILITY_ID_PATTERN)
    label: NonEmptyStr = Field(max_length=200)
    parents: tuple[str, ...] = ()

    @field_validator("parents")
    @classmethod
    def canonicalize_parents(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(value)))
        if any(
            not parent or len(parent) > 100 or re.fullmatch(_CAPABILITY_ID_PATTERN, parent) is None
            for parent in normalized
        ):
            raise ValueError("capability parents must contain safe capability IDs")
        return normalized


class CatalogEntry(ImmutableModel):
    """One repository observation and its zero or more capability assertions."""

    repository: RepositoryObservation
    assertions: tuple[CapabilityAssertion, ...] = ()
    rank: int | None = Field(default=None, gt=0, strict=True)

    @field_validator("assertions")
    @classmethod
    def canonicalize_assertions(
        cls, value: tuple[CapabilityAssertion, ...]
    ) -> tuple[CapabilityAssertion, ...]:
        capability_ids = [assertion.capability_id for assertion in value]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("duplicate capability_id in catalog entry")
        return tuple(sorted(value, key=lambda assertion: assertion.capability_id))

    @model_validator(mode="after")
    def validate_assertion_provenance(self) -> Self:
        repository_id = self.repository.identity.repository_id
        observation_hash = self.repository.stable_hash()
        for assertion in self.assertions:
            if assertion.repository_id != repository_id:
                raise ValueError("assertion repository_id does not match repository identity")
            if assertion.source_observation_hash != observation_hash:
                raise ValueError("assertion source_observation_hash does not match repository")
            if assertion.license_spdx != self.repository.license_spdx:
                raise ValueError("assertion license_spdx does not match repository observation")
            if assertion.reuse_status != self.repository.reuse_status:
                raise ValueError("assertion reuse_status does not match repository observation")
        return self


class CatalogManifest(ImmutableModel):
    """Deterministically ordered catalog publication manifest."""

    schema_version: NonEmptyStr = Field(max_length=100)
    taxonomy_version: NonEmptyStr = Field(max_length=100)
    classifier_version: NonEmptyStr = Field(max_length=100)
    generated_at: AwareDatetime | None = None
    source: NonEmptyStr = Field(default="explicit-observations", max_length=200)
    selection: CatalogSelectionCriteria | None = None
    api_total_count: int | None = Field(default=None, ge=0, strict=True)
    pages_fetched: int | None = Field(default=None, ge=0, strict=True)
    result_limit: int | None = Field(default=None, gt=0, le=1_000, strict=True)
    search_pages: tuple[CatalogSearchPageEvidence, ...] = ()
    cursor_start: int = Field(default=0, ge=0)
    cursor_end: int = Field(default=0, ge=0)
    discovered_count: int = Field(default=0, ge=0)
    validated_observation_count: int | None = Field(default=None, ge=0)
    pending_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    dead_letter_count: int = Field(default=0, ge=0)
    source_hashes: tuple[str, ...] = ()
    raw_page_hashes: tuple[str, ...] = ()
    classification_failure_repository_ids: tuple[int, ...] = ()
    coverage_complete: StrictBool = False
    coverage_note: NonEmptyStr = "Bounded discovery interval; not all public GitHub repositories."
    capability_definitions: tuple[CapabilityDefinition, ...] = ()
    entries: tuple[CatalogEntry, ...] = ()

    @field_validator("capability_definitions")
    @classmethod
    def canonicalize_capability_definitions(
        cls, value: tuple[CapabilityDefinition, ...]
    ) -> tuple[CapabilityDefinition, ...]:
        definition_ids = [definition.id for definition in value]
        if len(definition_ids) != len(set(definition_ids)):
            raise ValueError("duplicate capability definition ID")
        definitions = tuple(sorted(value, key=lambda definition: definition.id))
        _validate_capability_definition_graph(definitions)
        return definitions

    @field_validator("source_hashes", "raw_page_hashes")
    @classmethod
    def canonicalize_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SHA256_PATTERN.fullmatch(item) for item in value):
            raise ValueError("source hashes must be lowercase SHA-256 digests")
        return tuple(sorted(set(value)))

    @field_validator("search_pages")
    @classmethod
    def validate_ordered_search_pages(
        cls, value: tuple[CatalogSearchPageEvidence, ...]
    ) -> tuple[CatalogSearchPageEvidence, ...]:
        page_numbers = [page.page_number for page in value]
        if page_numbers != list(range(1, len(value) + 1)):
            raise ValueError("Search page evidence must be ordered and contiguous from page 1")
        raw_hashes = [page.raw_sha256 for page in value]
        if len(raw_hashes) != len(set(raw_hashes)):
            raise ValueError("Search page evidence must not repeat a raw response hash")
        return value

    @field_validator("classification_failure_repository_ids")
    @classmethod
    def canonicalize_failure_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(repository_id <= 0 for repository_id in value):
            raise ValueError("classification failure repository IDs must be positive")
        return tuple(sorted(set(value)))

    @field_validator("entries")
    @classmethod
    def canonicalize_entries(
        cls, value: tuple[CatalogEntry, ...], info: ValidationInfo
    ) -> tuple[CatalogEntry, ...]:
        repository_ids = [entry.repository.identity.repository_id for entry in value]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("duplicate repository_id in catalog manifest")
        if info.data.get("selection") is not None:
            return tuple(sorted(value, key=lambda entry: entry.rank or 0))
        return tuple(sorted(value, key=lambda entry: entry.repository.identity.repository_id))

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.cursor_end < self.cursor_start:
            raise ValueError("cursor_end cannot precede cursor_start")
        if self.validated_observation_count is not None and self.validated_observation_count < len(
            self.entries
        ):
            raise ValueError("validated observation count cannot be smaller than entry count")
        if self.capability_definitions:
            definition_ids = {definition.id for definition in self.capability_definitions}
            missing_targets = sorted(
                {
                    assertion.capability_id
                    for entry in self.entries
                    for assertion in entry.assertions
                    if assertion.capability_id not in definition_ids
                }
            )
            if missing_targets:
                raise ValueError(
                    f"assertion targets missing capability definitions: {missing_targets}"
                )
        self._validate_ranked_selection()
        return self

    def _validate_ranked_selection(self) -> None:
        metadata = (self.api_total_count, self.pages_fetched, self.result_limit)
        if self.selection is None:
            if (
                any(item is not None for item in metadata)
                or self.search_pages
                or any(entry.rank is not None for entry in self.entries)
            ):
                raise ValueError("rank and selection metadata require selection criteria")
            return
        if any(item is None for item in metadata):
            raise ValueError("ranked selection metadata must be complete")
        if self.result_limit != self.selection.result_limit:
            raise ValueError("result_limit must match the selection criteria")
        if self.result_limit is not None and len(self.entries) > self.result_limit:
            raise ValueError("ranked entry count cannot exceed result_limit")
        if self.api_total_count is not None and self.api_total_count < len(self.entries):
            raise ValueError("api_total_count cannot be smaller than ranked entry count")
        if self.search_pages:
            if self.pages_fetched != len(self.search_pages):
                raise ValueError("pages_fetched must match ordered Search page evidence")
            if set(self.raw_page_hashes) != {page.raw_sha256 for page in self.search_pages}:
                raise ValueError("raw_page_hashes must match ordered Search page evidence")
        if self.entries and self.pages_fetched == 0:
            raise ValueError("pages_fetched must be positive for a nonempty ranked catalog")

        ranks = [entry.rank for entry in self.entries]
        if ranks != list(range(1, len(self.entries) + 1)):
            raise ValueError("ranked catalog entries must have unique contiguous ranks")
        for entry in self.entries:
            repository = entry.repository
            if (
                repository.stargazers_count is None
                or repository.stargazers_count < self.selection.min_stars
                or repository.pushed_at is None
                or repository.pushed_at < self.selection.pushed_since
                or repository.archived is not False
                or repository.fork is not False
                or repository.private is not False
            ):
                raise ValueError("ranked catalog entry does not satisfy selection criteria")

        expected = sorted(
            self.entries,
            key=lambda entry: (
                -(entry.repository.stargazers_count or 0),
                entry.repository.identity.repository_id,
            ),
        )
        if list(self.entries) != expected:
            raise ValueError(
                "ranked catalog entries must follow stars descending then repository ID"
            )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capability_count(self) -> int:
        return len(
            {assertion.capability_id for entry in self.entries for assertion in entry.assertions}
        )


def _validate_capability_definition_graph(
    definitions: tuple[CapabilityDefinition, ...],
) -> None:
    definition_ids = {definition.id for definition in definitions}
    missing_parents = sorted(
        (definition.id, parent)
        for definition in definitions
        for parent in definition.parents
        if parent not in definition_ids
    )
    if missing_parents:
        raise ValueError(f"missing parent in capability definitions: {missing_parents}")

    parents_by_id = {definition.id: definition.parents for definition in definitions}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(capability_id: str) -> None:
        if capability_id in visiting:
            raise ValueError(f"parent cycle in capability definitions involving {capability_id!r}")
        if capability_id in visited:
            return
        visiting.add(capability_id)
        for parent in parents_by_id[capability_id]:
            visit(parent)
        visiting.remove(capability_id)
        visited.add(capability_id)

    for capability_id in sorted(definition_ids):
        visit(capability_id)
