"""Validated observation to deterministic catalog construction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from github_module_catalog.models import (
    CapabilityAssertion,
    CatalogEntry,
    CatalogManifest,
    RepositoryObservation,
)
from github_module_catalog.state import StateStore
from github_module_catalog.taxonomy import Taxonomy, classify_repository

Classifier = Callable[..., tuple[CapabilityAssertion, ...]]


@dataclass(frozen=True, slots=True)
class CatalogBuildContext:
    """Caller-supplied facts about the bounded discovery snapshot."""

    source: str = "explicit-observations"
    cursor_start: int = 0
    cursor_end: int = 0
    discovered_count: int = 0
    pending_count: int = 0
    retry_count: int = 0
    dead_letter_count: int = 0
    raw_page_hashes: tuple[str, ...] = ()
    coverage_complete: bool = False
    coverage_note: str = "Bounded discovery interval; not all public GitHub repositories."


def build_catalog(
    observations: tuple[RepositoryObservation, ...],
    *,
    taxonomy: Taxonomy,
    context: CatalogBuildContext,
    classifier_version: str = "rules-v1",
    generated_at: datetime | None = None,
    classifier: Classifier = classify_repository,
    schema_version: str = "1.0.0",
) -> CatalogManifest:
    """Classify validated facts, isolating a failure to its repository entry."""

    _validate_observations(observations)
    entries: list[CatalogEntry] = []
    failure_ids: list[int] = []
    for observation in observations:
        try:
            assertions = classifier(
                observation,
                taxonomy,
                classifier_version=classifier_version,
            )
        except Exception:
            assertions = ()
            failure_ids.append(observation.identity.repository_id)
        entries.append(CatalogEntry(repository=observation, assertions=assertions))
    return CatalogManifest(
        schema_version=schema_version,
        taxonomy_version=taxonomy.version,
        classifier_version=classifier_version,
        generated_at=generated_at,
        source=context.source,
        cursor_start=context.cursor_start,
        cursor_end=context.cursor_end,
        discovered_count=context.discovered_count,
        validated_observation_count=len(observations),
        pending_count=context.pending_count,
        retry_count=context.retry_count,
        dead_letter_count=context.dead_letter_count,
        source_hashes=tuple(observation.stable_hash() for observation in observations),
        raw_page_hashes=context.raw_page_hashes,
        classification_failure_repository_ids=tuple(failure_ids),
        coverage_complete=context.coverage_complete,
        coverage_note=context.coverage_note,
        entries=tuple(entries),
    )


def build_catalog_from_state(
    state: StateStore,
    *,
    taxonomy: Taxonomy,
    context: CatalogBuildContext,
    classifier_version: str = "rules-v1",
    generated_at: datetime | None = None,
    classifier: Classifier = classify_repository,
    schema_version: str = "1.0.0",
) -> CatalogManifest:
    """Build from the state's latest hash-verified observations."""

    return build_catalog(
        state.list_latest_repository_observations(),
        taxonomy=taxonomy,
        context=context,
        classifier_version=classifier_version,
        generated_at=generated_at,
        classifier=classifier,
        schema_version=schema_version,
    )


def _validate_observations(observations: tuple[RepositoryObservation, ...]) -> None:
    if not isinstance(observations, tuple):
        raise TypeError("observations must be an immutable tuple")
    if any(not isinstance(item, RepositoryObservation) for item in observations):
        raise TypeError("observations must contain only validated RepositoryObservation objects")
    repository_ids = [item.identity.repository_id for item in observations]
    if len(repository_ids) != len(set(repository_ids)):
        raise ValueError("observations must contain one latest fact set per repository")
