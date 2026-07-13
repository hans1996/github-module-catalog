"""Versioned taxonomy loading and deterministic repository classification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from typing import Self

import yaml  # type: ignore[import-untyped]
from pydantic import Field, field_validator, model_validator

from github_module_catalog.models import (
    CapabilityAssertion,
    Evidence,
    ImmutableModel,
    NonEmptyStr,
    RepositoryObservation,
)

_TOKEN_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9+#.-]*[a-z0-9+#])?")
_AXIS_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
_NODE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"


def _canonical_strings(values: object, *, casefold: bool = False) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("expected a collection of strings")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("expected non-empty strings")
        item = value.strip()
        normalized.add(item.casefold() if casefold else item)
    return tuple(sorted(normalized))


def _normalized_phrase(value: str) -> str:
    separated = re.sub(r"[-_/]+", " ", value.casefold())
    return " ".join(_TOKEN_PATTERN.findall(separated))


def _canonical_phrases(values: object) -> tuple[str, ...]:
    phrases = {_normalized_phrase(value) for value in _canonical_strings(values, casefold=True)}
    if any(not phrase for phrase in phrases):
        raise ValueError("description phrases must contain searchable text")
    return tuple(sorted(phrases))


class TaxonomyNode(ImmutableModel):
    """Stable node on one taxonomy axis."""

    id: str = Field(pattern=_NODE_ID_PATTERN)
    label: NonEmptyStr
    aliases: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    inclusion_examples: tuple[NonEmptyStr, ...] = Field(min_length=1)
    exclusion_examples: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, value: object) -> tuple[str, ...]:
        return _canonical_strings(value, casefold=True)

    @field_validator("parents", "inclusion_examples", "exclusion_examples", mode="before")
    @classmethod
    def canonicalize_text_collections(cls, value: object) -> tuple[str, ...]:
        return _canonical_strings(value)


class TaxonomyAxis(ImmutableModel):
    """Named, immutable collection of taxonomy nodes."""

    id: str = Field(pattern=_AXIS_ID_PATTERN)
    nodes: tuple[TaxonomyNode, ...] = Field(min_length=1)

    @field_validator("nodes")
    @classmethod
    def validate_unique_nodes(cls, value: tuple[TaxonomyNode, ...]) -> tuple[TaxonomyNode, ...]:
        node_ids = [node.id for node in value]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("taxonomy node IDs must be unique within an axis")
        return tuple(sorted(value, key=lambda node: node.id))


class ClassificationRule(ImmutableModel):
    """Deterministic match signals for one capability node."""

    capability_id: str = Field(pattern=_NODE_ID_PATTERN)
    topics: tuple[str, ...] = ()
    description_tokens: tuple[str, ...] = ()
    description_phrases: tuple[str, ...] = ()
    language_hints: tuple[str, ...] = ()
    exclude_tokens: tuple[str, ...] = ()
    exclude_topics: tuple[str, ...] = ()

    @field_validator(
        "topics",
        "description_tokens",
        "language_hints",
        "exclude_tokens",
        "exclude_topics",
        mode="before",
    )
    @classmethod
    def normalize_match_values(cls, value: object) -> tuple[str, ...]:
        return _canonical_strings(value, casefold=True)

    @field_validator("description_phrases", mode="before")
    @classmethod
    def normalize_description_phrases(cls, value: object) -> tuple[str, ...]:
        return _canonical_phrases(value)

    @model_validator(mode="after")
    def require_positive_signal(self) -> Self:
        if not self.topics and not self.description_tokens and not self.description_phrases:
            raise ValueError(
                "classification rule requires a topic, description token, or description phrase"
            )
        return self


def _validate_axis_parent_graph(axis: TaxonomyAxis) -> None:
    node_ids = {node.id for node in axis.nodes}
    missing_parents = sorted(
        (node.id, parent)
        for node in axis.nodes
        for parent in node.parents
        if parent not in node_ids
    )
    if missing_parents:
        raise ValueError(f"missing parent on axis {axis.id!r}: {missing_parents}")

    parents_by_node = {node.id: node.parents for node in axis.nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"parent cycle on axis {axis.id!r} involving {node_id!r}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for parent in parents_by_node[node_id]:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(node_ids):
        visit(node_id)


class Taxonomy(ImmutableModel):
    """Validated taxonomy document loaded from versioned YAML."""

    version: NonEmptyStr
    resource_exclusion_topics: tuple[str, ...] = ()
    resource_exclusion_name_prefixes: tuple[str, ...] = ()
    resource_exclusion_phrases: tuple[str, ...] = ()
    axis_definitions: tuple[TaxonomyAxis, ...] = Field(validation_alias="axes")
    rules: tuple[ClassificationRule, ...]

    @field_validator("resource_exclusion_topics", mode="before")
    @classmethod
    def normalize_resource_exclusion_topics(cls, value: object) -> tuple[str, ...]:
        return _canonical_strings(value, casefold=True)

    @field_validator("resource_exclusion_name_prefixes", mode="before")
    @classmethod
    def normalize_resource_exclusion_name_prefixes(cls, value: object) -> tuple[str, ...]:
        return _canonical_phrases(value)

    @field_validator("resource_exclusion_phrases", mode="before")
    @classmethod
    def normalize_resource_exclusion_phrases(cls, value: object) -> tuple[str, ...]:
        return _canonical_phrases(value)

    @field_validator("axis_definitions", mode="before")
    @classmethod
    def unpack_axes(cls, value: object) -> list[dict[str, object]]:
        if not isinstance(value, dict):
            raise ValueError("axes must be a mapping of axis IDs to node lists")
        return [{"id": axis_id, "nodes": nodes} for axis_id, nodes in value.items()]

    @field_validator("axis_definitions")
    @classmethod
    def canonicalize_axes(cls, value: tuple[TaxonomyAxis, ...]) -> tuple[TaxonomyAxis, ...]:
        axis_ids = [axis.id for axis in value]
        if len(axis_ids) != len(set(axis_ids)):
            raise ValueError("taxonomy axis IDs must be unique")
        return tuple(sorted(value, key=lambda axis: axis.id))

    @field_validator("rules")
    @classmethod
    def canonicalize_rules(
        cls, value: tuple[ClassificationRule, ...]
    ) -> tuple[ClassificationRule, ...]:
        capability_ids = [rule.capability_id for rule in value]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("a capability may have only one deterministic rule")
        return tuple(sorted(value, key=lambda rule: rule.capability_id))

    @model_validator(mode="after")
    def validate_rule_targets(self) -> Self:
        for axis in self.axis_definitions:
            _validate_axis_parent_graph(axis)
        capability_ids = {node.id for node in self.axes.get("capability", ())}
        missing = sorted(
            rule.capability_id for rule in self.rules if rule.capability_id not in capability_ids
        )
        if missing:
            raise ValueError(f"rules reference missing capability nodes: {missing}")
        return self

    @property
    def axes(self) -> Mapping[str, tuple[TaxonomyNode, ...]]:
        """Expose a read-only axis lookup without retaining a mutable input mapping."""

        return MappingProxyType({axis.id: axis.nodes for axis in self.axis_definitions})


def load_taxonomy(path: str | Path | Traversable) -> Taxonomy:
    """Load and strictly validate a versioned YAML taxonomy document."""

    taxonomy_source = Path(path) if isinstance(path, (str, Path)) else path
    with taxonomy_source.open(encoding="utf-8") as taxonomy_file:
        raw_document = yaml.safe_load(taxonomy_file)
    if not isinstance(raw_document, dict):
        raise ValueError("taxonomy document must contain a mapping at its root")
    return Taxonomy.model_validate(raw_document)


def _description_tokens(description: str | None) -> frozenset[str]:
    if description is None:
        return frozenset()
    return frozenset(_TOKEN_PATTERN.findall(description.casefold()))


def _description_phrase_text(description: str | None) -> str:
    return "" if description is None else _normalized_phrase(description)


def _lifecycle_value(observation: RepositoryObservation) -> str:
    if observation.disabled is True:
        return "disabled"
    if observation.archived is True:
        return "archived"
    if observation.disabled is False and observation.archived is False:
        return "active"
    return "unknown"


def _is_resource_only(observation: RepositoryObservation, taxonomy: Taxonomy) -> bool:
    if frozenset(observation.topics).intersection(taxonomy.resource_exclusion_topics):
        return True
    normalized_name = _normalized_phrase(observation.name)
    if any(
        normalized_name == prefix or normalized_name.startswith(f"{prefix} ")
        for prefix in taxonomy.resource_exclusion_name_prefixes
    ):
        return True
    description = _normalized_phrase(observation.description or "")
    padded_description = f" {description} "
    return any(
        f" {phrase} " in padded_description for phrase in taxonomy.resource_exclusion_phrases
    )


def _evidence_and_confidence(
    observation: RepositoryObservation,
    rule: ClassificationRule,
) -> tuple[tuple[Evidence, ...], float] | None:
    repository_topics = frozenset(observation.topics)
    description_tokens = _description_tokens(observation.description)
    if repository_topics.intersection(rule.exclude_topics) or description_tokens.intersection(
        rule.exclude_tokens
    ):
        return None

    topic_matches = repository_topics.intersection(rule.topics)
    description_matches = description_tokens.intersection(rule.description_tokens)
    phrase_text = _description_phrase_text(observation.description)
    padded_phrase_text = f" {phrase_text} "
    phrase_matches = frozenset(
        phrase for phrase in rule.description_phrases if f" {phrase} " in padded_phrase_text
    )
    if not topic_matches and not description_matches and not phrase_matches:
        return None

    evidence = [Evidence(source="topic", value=value) for value in sorted(topic_matches)]
    evidence.extend(
        Evidence(source="description", value=value) for value in sorted(description_matches)
    )
    evidence.extend(Evidence(source="description", value=value) for value in sorted(phrase_matches))
    confidence = 0.95 if topic_matches else 0.75
    if topic_matches and (description_matches or phrase_matches):
        confidence += 0.02

    language = observation.primary_language.casefold() if observation.primary_language else None
    if language is not None and language in rule.language_hints:
        evidence.append(Evidence(source="language", value=observation.primary_language or language))
        confidence += 0.02

    evidence.append(Evidence(source="lifecycle", value=_lifecycle_value(observation)))
    evidence.append(Evidence(source="license", value=observation.license_spdx or "missing"))
    return tuple(evidence), min(confidence, 0.99)


def _capability_ancestors(taxonomy: Taxonomy, capability_id: str) -> tuple[str, ...]:
    parents_by_id = {node.id: node.parents for node in taxonomy.axes.get("capability", ())}
    ancestors: set[str] = set()
    pending = list(parents_by_id.get(capability_id, ()))
    while pending:
        parent = pending.pop()
        if parent in ancestors:
            continue
        ancestors.add(parent)
        pending.extend(parents_by_id[parent])
    return tuple(sorted(ancestors))


def _resolved_capability_matches(
    observation: RepositoryObservation,
    taxonomy: Taxonomy,
) -> dict[str, tuple[tuple[Evidence, ...], float]]:
    if _is_resource_only(observation, taxonomy):
        return {}
    direct: dict[str, tuple[tuple[Evidence, ...], float]] = {}
    for rule in taxonomy.rules:
        result = _evidence_and_confidence(observation, rule)
        if result is not None:
            direct[rule.capability_id] = result

    inherited: dict[str, tuple[str, tuple[Evidence, ...], float]] = {}
    for descendant_id, (evidence, confidence) in sorted(direct.items()):
        derived_evidence = (
            *evidence,
            Evidence(source="taxonomy", value=f"derived-from:{descendant_id}"),
        )
        for ancestor_id in _capability_ancestors(taxonomy, descendant_id):
            if ancestor_id in direct:
                continue
            candidate = (descendant_id, derived_evidence, confidence)
            current = inherited.get(ancestor_id)
            if current is None or (-confidence, descendant_id) < (-current[2], current[0]):
                inherited[ancestor_id] = candidate

    resolved = dict(direct)
    resolved.update(
        {
            capability_id: (evidence, confidence)
            for capability_id, (_, evidence, confidence) in inherited.items()
        }
    )
    return resolved


def classify_repository(
    observation: RepositoryObservation,
    taxonomy: Taxonomy,
    *,
    classifier_version: str = "rules-v2",
) -> tuple[CapabilityAssertion, ...]:
    """Create deterministic, multi-label assertions without mutating source facts."""

    assertions: list[CapabilityAssertion] = []
    observation_hash = observation.stable_hash()
    for capability_id, (evidence, confidence) in sorted(
        _resolved_capability_matches(observation, taxonomy).items()
    ):
        assertions.append(
            CapabilityAssertion(
                repository_id=observation.identity.repository_id,
                capability_id=capability_id,
                taxonomy_version=taxonomy.version,
                classifier_version=classifier_version,
                confidence=confidence,
                evidence=evidence,
                source_observation_hash=observation_hash,
                license_spdx=observation.license_spdx,
                reuse_status=observation.reuse_status,
            )
        )
    return tuple(assertions)
