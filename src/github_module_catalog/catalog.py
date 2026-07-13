"""Validated observation to deterministic catalog construction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from github_module_catalog.models import (
    CapabilityAssertion,
    CapabilityDefinition,
    CatalogEntry,
    CatalogManifest,
    CatalogSearchPageEvidence,
    CatalogSelectionCriteria,
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
    selection: CatalogSelectionCriteria | None = None
    api_total_count: int | None = None
    pages_fetched: int | None = None
    result_limit: int | None = None
    repository_ranks: tuple[tuple[int, int], ...] = ()
    search_pages: tuple[CatalogSearchPageEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.search_pages, tuple) or any(
            not isinstance(item, CatalogSearchPageEvidence) for item in self.search_pages
        ):
            raise TypeError("search_pages must be an immutable tuple of validated evidence")
        if self.selection is not None and not isinstance(self.selection, CatalogSelectionCriteria):
            raise TypeError("selection must be an immutable CatalogSelectionCriteria")
        if not isinstance(self.repository_ranks, tuple) or any(
            not isinstance(item, tuple) or len(item) != 2 for item in self.repository_ranks
        ):
            raise TypeError("repository_ranks must be an immutable tuple of pairs")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for pair in self.repository_ranks
            for value in pair
        ):
            raise TypeError("repository rank pairs must contain only strict integers")
        repository_ids = [repository_id for repository_id, _ in self.repository_ranks]
        ranks = [rank for _, rank in self.repository_ranks]
        if any(repository_id <= 0 for repository_id in repository_ids):
            raise ValueError("rank mapping repository IDs must be positive")
        if len(repository_ids) != len(set(repository_ids)) or sorted(ranks) != list(
            range(1, len(ranks) + 1)
        ):
            raise ValueError("rank mapping must contain unique contiguous ranks")
        metadata = (self.api_total_count, self.pages_fetched, self.result_limit)
        if self.selection is None:
            if (
                self.repository_ranks
                or self.search_pages
                or any(item is not None for item in metadata)
            ):
                raise ValueError("rank mapping and Search metadata require selection criteria")
            return
        if any(item is None for item in metadata):
            raise ValueError("ranked selection metadata must be complete")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in metadata):
            raise TypeError("ranked selection metadata must contain strict integers")
        if self.api_total_count is not None and self.api_total_count < 0:
            raise ValueError("api_total_count must be nonnegative")
        if self.pages_fetched is not None and self.pages_fetched < 0:
            raise ValueError("pages_fetched must be nonnegative")
        if self.result_limit != self.selection.result_limit:
            raise ValueError("result_limit must match the selection criteria")
        if self.search_pages and self.pages_fetched != len(self.search_pages):
            raise ValueError("pages_fetched must match ordered Search page evidence")


def build_catalog(
    observations: tuple[RepositoryObservation, ...],
    *,
    taxonomy: Taxonomy,
    context: CatalogBuildContext,
    classifier_version: str = "rules-v1",
    generated_at: datetime | None = None,
    classifier: Classifier = classify_repository,
    schema_version: str = "1.1.0",
) -> CatalogManifest:
    """Classify validated facts, isolating a failure to its repository entry."""

    _validate_observations(observations)
    rank_by_repository = _rank_mapping(observations, context)
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
        entries.append(
            CatalogEntry(
                repository=observation,
                assertions=assertions,
                rank=rank_by_repository.get(observation.identity.repository_id),
            )
        )
    return CatalogManifest(
        schema_version=schema_version,
        taxonomy_version=taxonomy.version,
        classifier_version=classifier_version,
        generated_at=generated_at,
        source=context.source,
        selection=context.selection,
        api_total_count=context.api_total_count,
        pages_fetched=context.pages_fetched,
        result_limit=context.result_limit,
        search_pages=context.search_pages,
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
        capability_definitions=tuple(
            CapabilityDefinition(id=node.id, label=node.label, parents=node.parents)
            for node in taxonomy.axes.get("capability", ())
        ),
        entries=tuple(entries),
    )


def build_catalog_from_state(
    state: StateStore,
    *,
    taxonomy: Taxonomy,
    source: str,
    classifier_version: str = "rules-v1",
    generated_at: datetime | None = None,
    classifier: Classifier = classify_repository,
    schema_version: str = "1.1.0",
) -> CatalogManifest:
    """Build from the state's latest hash-verified observations."""

    snapshot = state.catalog_snapshot(source)
    return build_catalog(
        snapshot.observations,
        taxonomy=taxonomy,
        context=CatalogBuildContext(
            source=snapshot.source,
            cursor_start=snapshot.cursor_start,
            cursor_end=snapshot.cursor_end,
            discovered_count=snapshot.discovered_count,
            pending_count=snapshot.pending_count,
            retry_count=snapshot.retry_count,
            dead_letter_count=snapshot.dead_letter_count,
            raw_page_hashes=snapshot.raw_page_hashes,
        ),
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


def _rank_mapping(
    observations: tuple[RepositoryObservation, ...], context: CatalogBuildContext
) -> dict[int, int]:
    if context.selection is None:
        return {}
    rank_by_repository = dict(context.repository_ranks)
    repository_ids = {observation.identity.repository_id for observation in observations}
    if set(rank_by_repository) != repository_ids:
        raise ValueError("rank mapping must contain exactly one rank for each observation")
    return rank_by_repository
