"""Versioned taxonomy loading and deterministic repository classification."""

from __future__ import annotations

import re
from collections.abc import Mapping
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

_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
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
    language_hints: tuple[str, ...] = ()
    exclude_tokens: tuple[str, ...] = ()

    @field_validator(
        "topics", "description_tokens", "language_hints", "exclude_tokens", mode="before"
    )
    @classmethod
    def normalize_match_values(cls, value: object) -> tuple[str, ...]:
        return _canonical_strings(value, casefold=True)

    @model_validator(mode="after")
    def require_positive_signal(self) -> Self:
        if not self.topics and not self.description_tokens:
            raise ValueError("classification rule requires a topic or description token")
        return self


class Taxonomy(ImmutableModel):
    """Validated taxonomy document loaded from versioned YAML."""

    version: NonEmptyStr
    axis_definitions: tuple[TaxonomyAxis, ...] = Field(validation_alias="axes")
    rules: tuple[ClassificationRule, ...]

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


def load_taxonomy(path: str | Path) -> Taxonomy:
    """Load and strictly validate a versioned YAML taxonomy document."""

    with Path(path).open(encoding="utf-8") as taxonomy_file:
        raw_document = yaml.safe_load(taxonomy_file)
    if not isinstance(raw_document, dict):
        raise ValueError("taxonomy document must contain a mapping at its root")
    return Taxonomy.model_validate(raw_document)


def _description_tokens(description: str | None) -> frozenset[str]:
    if description is None:
        return frozenset()
    return frozenset(_TOKEN_PATTERN.findall(description.casefold()))


def _lifecycle_value(observation: RepositoryObservation) -> str:
    if observation.disabled:
        return "disabled"
    if observation.archived:
        return "archived"
    return "active"


def _evidence_and_confidence(
    observation: RepositoryObservation,
    rule: ClassificationRule,
) -> tuple[tuple[Evidence, ...], float] | None:
    repository_topics = frozenset(observation.topics)
    description_tokens = _description_tokens(observation.description)
    if description_tokens.intersection(rule.exclude_tokens):
        return None

    topic_matches = repository_topics.intersection(rule.topics)
    description_matches = description_tokens.intersection(rule.description_tokens)
    if not topic_matches and not description_matches:
        return None

    evidence = [Evidence(source="topic", value=value) for value in sorted(topic_matches)]
    evidence.extend(
        Evidence(source="description", value=value) for value in sorted(description_matches)
    )
    confidence = 0.95 if topic_matches else 0.75
    if topic_matches and description_matches:
        confidence += 0.02

    language = observation.primary_language.casefold() if observation.primary_language else None
    if language is not None and language in rule.language_hints:
        evidence.append(Evidence(source="language", value=observation.primary_language or language))
        confidence += 0.02

    evidence.append(Evidence(source="lifecycle", value=_lifecycle_value(observation)))
    evidence.append(Evidence(source="license", value=observation.license_spdx or "missing"))
    return tuple(evidence), min(confidence, 0.99)


def classify_repository(
    observation: RepositoryObservation,
    taxonomy: Taxonomy,
    *,
    classifier_version: str = "rules-v1",
) -> tuple[CapabilityAssertion, ...]:
    """Create deterministic, multi-label assertions without mutating source facts."""

    assertions: list[CapabilityAssertion] = []
    observation_hash = observation.stable_hash()
    for rule in taxonomy.rules:
        result = _evidence_and_confidence(observation, rule)
        if result is None:
            continue
        evidence, confidence = result
        assertions.append(
            CapabilityAssertion(
                repository_id=observation.identity.repository_id,
                capability_id=rule.capability_id,
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
