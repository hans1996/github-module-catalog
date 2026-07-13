"""Immutable domain models for repository observations and catalog output."""

from __future__ import annotations

import hashlib
import json
import re
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
            self.model_dump(mode="json", exclude_computed_fields=True),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

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


class RepositoryObservation(ImmutableModel):
    """Validated source facts observed for a public GitHub repository."""

    identity: RepositoryIdentity
    owner: str = Field(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    full_name: str = Field(min_length=3, max_length=140)
    html_url: HttpUrl
    description: str | None = Field(default=None, max_length=10_000)
    topics: tuple[str, ...] = ()
    primary_language: str | None = Field(default=None, min_length=1, max_length=100)
    created_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    pushed_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    archived: StrictBool | None = None
    disabled: StrictBool | None = None
    fork: StrictBool | None = None
    license_spdx: str | None = None
    license_name: str | None = Field(default=None, min_length=1, max_length=500)

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


EvidenceSource = Literal["topic", "description", "language", "lifecycle", "license"]


class Evidence(ImmutableModel):
    """A source fact that supports a classifier assertion."""

    source: EvidenceSource
    value: NonEmptyStr = Field(max_length=1_000)


class CapabilityAssertion(ImmutableModel):
    """Versioned, traceable claim that a repository provides a capability."""

    repository_id: int = Field(gt=0)
    capability_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
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


class CatalogEntry(ImmutableModel):
    """One repository observation and its zero or more capability assertions."""

    repository: RepositoryObservation
    assertions: tuple[CapabilityAssertion, ...] = ()

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
    entries: tuple[CatalogEntry, ...] = ()

    @field_validator("source_hashes", "raw_page_hashes")
    @classmethod
    def canonicalize_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SHA256_PATTERN.fullmatch(item) for item in value):
            raise ValueError("source hashes must be lowercase SHA-256 digests")
        return tuple(sorted(set(value)))

    @field_validator("classification_failure_repository_ids")
    @classmethod
    def canonicalize_failure_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(repository_id <= 0 for repository_id in value):
            raise ValueError("classification failure repository IDs must be positive")
        return tuple(sorted(set(value)))

    @field_validator("entries")
    @classmethod
    def canonicalize_entries(cls, value: tuple[CatalogEntry, ...]) -> tuple[CatalogEntry, ...]:
        repository_ids = [entry.repository.identity.repository_id for entry in value]
        if len(repository_ids) != len(set(repository_ids)):
            raise ValueError("duplicate repository_id in catalog manifest")
        return tuple(sorted(value, key=lambda entry: entry.repository.identity.repository_id))

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.cursor_end < self.cursor_start:
            raise ValueError("cursor_end cannot precede cursor_start")
        if self.validated_observation_count is not None and self.validated_observation_count < len(
            self.entries
        ):
            raise ValueError("validated observation count cannot be smaller than entry count")
        return self

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
