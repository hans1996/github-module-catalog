from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from github_module_catalog.models import Evidence, RepositoryObservation, ReuseStatus
from github_module_catalog.taxonomy import Taxonomy, classify_repository, load_taxonomy

TAXONOMY_PATH = (
    Path(__file__).parents[2] / "src" / "github_module_catalog" / "data" / "taxonomy.yaml"
)
REQUIRED_CAPABILITIES = {
    "cli",
    "web-ui",
    "api-backend",
    "auth",
    "database-storage",
    "ai-ml",
    "testing",
    "devops",
    "observability",
    "media",
    "security",
}
V2_LEAF_PARENTS: dict[str, tuple[str, ...]] = {
    "terminal-ui": ("cli",),
    "terminal-emulator": ("cli",),
    "shell-tooling": ("cli",),
    "package-manager": ("cli",),
    "ui-component-library": ("web-ui",),
    "dashboard-ui": ("web-ui",),
    "static-site-generator": ("web-ui",),
    "content-management": ("web-ui",),
    "rest-api": ("api-backend",),
    "graphql-api": ("api-backend",),
    "rpc-api": ("api-backend",),
    "realtime-api": ("api-backend",),
    "api-gateway": ("api-backend",),
    "oauth-oidc": ("auth",),
    "identity-provider": ("auth",),
    "access-control": ("auth",),
    "multi-factor-auth": ("auth",),
    "relational-database": ("database-storage",),
    "document-database": ("database-storage",),
    "cache-key-value": ("database-storage",),
    "vector-database": ("database-storage",),
    "object-storage": ("database-storage",),
    "search-engine": ("database-storage",),
    "llm-runtime": ("ai-ml",),
    "ai-agent-framework": ("ai-ml",),
    "rag-retrieval": ("ai-ml",),
    "model-training": ("ai-ml",),
    "computer-vision": ("ai-ml", "media"),
    "speech-ai": ("ai-ml", "media"),
    "unit-test-framework": ("testing",),
    "browser-e2e-testing": ("testing",),
    "api-testing": ("testing",),
    "performance-testing": ("testing",),
    "ci-cd": ("devops",),
    "container-tooling": ("devops",),
    "kubernetes-tooling": ("devops",),
    "infrastructure-as-code": ("devops",),
    "configuration-management": ("devops",),
    "metrics-monitoring": ("observability",),
    "log-management": ("observability",),
    "distributed-tracing": ("observability",),
    "error-tracking": ("observability",),
    "profiling": ("observability",),
    "image-processing": ("media",),
    "video-processing": ("media",),
    "audio-processing": ("media",),
    "media-streaming": ("media",),
    "media-downloader": ("media",),
    "vulnerability-scanning": ("security",),
    "penetration-testing": ("security",),
    "cryptography": ("security",),
    "secrets-management": ("security",),
    "network-security": ("security",),
    "reverse-engineering": ("security",),
    "malware-analysis": ("security",),
}


def repository_fixture(**overrides: object) -> RepositoryObservation:
    values: dict[str, Any] = {
        "identity": {"repository_id": 42},
        "owner": "octocat",
        "name": "toolkit",
        "full_name": "octocat/toolkit",
        "html_url": "https://github.com/octocat/toolkit",
        "description": "Authentication REST API and command-line toolkit",
        "topics": ["Auth", "CLI", "api"],
        "primary_language": "Python",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2024, 2, 1, tzinfo=UTC),
        "pushed_at": None,
        "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        "archived": False,
        "disabled": False,
        "fork": False,
        "license_spdx": "MIT",
        "license_name": "MIT License",
    }
    return RepositoryObservation(**(values | overrides))


def test_loads_versioned_multi_axis_taxonomy_with_stable_nodes() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)

    assert taxonomy.version == "2.0.0"
    assert {
        "artifact_type",
        "capability",
        "domain",
        "runtime",
        "interface",
        "ecosystem",
        "lifecycle",
        "license",
    }.issubset(taxonomy.axes)
    capabilities = taxonomy.axes["capability"]
    assert REQUIRED_CAPABILITIES.issubset({node.id for node in capabilities})
    assert all(node.inclusion_examples and node.exclusion_examples for node in capabilities)
    assert all(node.aliases is not None and node.parents is not None for node in capabilities)


def test_packaged_v2_preserves_parents_and_defines_every_leaf_rule() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    nodes_by_id = {node.id: node for node in taxonomy.axes["capability"]}
    rules_by_id = {rule.capability_id: rule for rule in taxonomy.rules}

    assert REQUIRED_CAPABILITIES.issubset(nodes_by_id)
    assert set(V2_LEAF_PARENTS).issubset(nodes_by_id)
    assert set(V2_LEAF_PARENTS).issubset(rules_by_id)
    assert len(V2_LEAF_PARENTS) == 55
    for capability_id, parents in V2_LEAF_PARENTS.items():
        assert nodes_by_id[capability_id].parents == parents
        assert rules_by_id[capability_id].topics


@pytest.mark.parametrize("capability_id", sorted(V2_LEAF_PARENTS))
def test_every_packaged_v2_leaf_has_a_working_topic_signal(capability_id: str) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    rule = next(rule for rule in taxonomy.rules if rule.capability_id == capability_id)
    observation = repository_fixture(
        description=None,
        topics=[rule.topics[0]],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert capability_id in capability_ids
    assert set(V2_LEAF_PARENTS[capability_id]).issubset(capability_ids)


@pytest.mark.parametrize(
    ("topic", "forbidden_capability"),
    [
        ("terminal", "terminal-emulator"),
        ("docker", "container-tooling"),
        ("kubernetes", "kubernetes-tooling"),
        ("crypto", "cryptography"),
    ],
)
def test_packaged_v2_avoids_ambiguous_leaf_topics(
    topic: str,
    forbidden_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(description=None, topics=[topic], primary_language=None)

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert forbidden_capability not in capability_ids


@pytest.mark.parametrize(
    ("signal_topic", "noise_topic", "forbidden_capability"),
    [
        ("ai-agent", "awesome-list", "ai-agent-framework"),
        ("postgresql", "tutorials", "relational-database"),
        ("penetration-testing", "course", "penetration-testing"),
    ],
)
def test_packaged_v2_rejects_resource_only_projects(
    signal_topic: str,
    noise_topic: str,
    forbidden_capability: str,
) -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description=None,
        topics=[signal_topic, noise_topic],
        primary_language=None,
    )

    capability_ids = {
        assertion.capability_id for assertion in classify_repository(observation, taxonomy)
    }

    assert forbidden_capability not in capability_ids


def test_taxonomy_rejects_unknown_configuration_fields(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 1.0.0\naxes: {}\nrules: []\nunexpected: true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_taxonomy(invalid)


def taxonomy_node(node_id: str, *, parents: list[str] | None = None) -> dict[str, object]:
    return {
        "id": node_id,
        "label": node_id,
        "aliases": [],
        "parents": parents or [],
        "inclusion_examples": [f"includes {node_id}"],
        "exclusion_examples": [f"excludes {node_id}"],
    }


def focused_taxonomy(
    nodes: list[dict[str, object]],
    rules: list[dict[str, object]],
) -> Taxonomy:
    return Taxonomy.model_validate(
        {
            "version": "2.0.0",
            "axes": {"capability": nodes},
            "rules": rules,
        }
    )


def test_taxonomy_rejects_parent_from_a_different_axis() -> None:
    document = {
        "version": "1.0.0",
        "axes": {
            "capability": [taxonomy_node("child", parents=["shared-parent"])],
            "domain": [taxonomy_node("shared-parent")],
        },
        "rules": [],
    }

    with pytest.raises(ValidationError, match="missing parent"):
        Taxonomy.model_validate(document)


@pytest.mark.parametrize(
    "nodes",
    [
        [taxonomy_node("self-parent", parents=["self-parent"])],
        [taxonomy_node("first", parents=["second"]), taxonomy_node("second", parents=["first"])],
    ],
)
def test_taxonomy_rejects_parent_cycles(nodes: list[dict[str, object]]) -> None:
    document = {
        "version": "1.0.0",
        "axes": {"capability": nodes},
        "rules": [],
    }

    with pytest.raises(ValidationError, match="parent cycle"):
        Taxonomy.model_validate(document)


def test_classifier_returns_deterministic_multi_label_assertions_with_provenance() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture()

    assertions = classify_repository(observation, taxonomy, classifier_version="rules-v2")

    assert [assertion.capability_id for assertion in assertions] == [
        "api-backend",
        "auth",
        "cli",
        "rest-api",
        "security",
    ]
    assert all(assertion.repository_id == 42 for assertion in assertions)
    assert all(assertion.taxonomy_version == "2.0.0" for assertion in assertions)
    assert all(assertion.classifier_version == "rules-v2" for assertion in assertions)
    assert all(
        assertion.source_observation_hash == observation.stable_hash() for assertion in assertions
    )
    assert all(0.0 <= assertion.confidence <= 1.0 for assertion in assertions)
    assert all(assertion.evidence for assertion in assertions)
    assert all(assertion.reuse_status == ReuseStatus.SAFE_TO_INTEGRATE for assertion in assertions)
    assert any(
        evidence.source == "language" for assertion in assertions for evidence in assertion.evidence
    )
    security = next(assertion for assertion in assertions if assertion.capability_id == "security")
    assert any(
        evidence.source == "taxonomy" and evidence.value == "derived-from:auth"
        for evidence in security.evidence
    )


def test_classifier_uses_lifecycle_and_license_as_reuse_gates() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    archived = repository_fixture(archived=True, license_spdx="MIT")
    unlicensed = repository_fixture(license_spdx="NOASSERTION")

    archived_assertions = classify_repository(archived, taxonomy)
    unlicensed_assertions = classify_repository(unlicensed, taxonomy)

    assert archived_assertions
    assert unlicensed_assertions
    assert all(
        assertion.reuse_status == ReuseStatus.DISCOVERY_ONLY
        for assertion in (*archived_assertions, *unlicensed_assertions)
    )
    assert any(
        evidence.source == "lifecycle"
        for assertion in archived_assertions
        for evidence in assertion.evidence
    )
    assert any(
        evidence.source == "license"
        for assertion in unlicensed_assertions
        for evidence in assertion.evidence
    )


def test_classifier_is_stable_and_does_not_mutate_inputs() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    topics = ["CLI", "auth", "api"]
    observation = repository_fixture(topics=topics)
    original_topics = list(topics)

    first = classify_repository(observation, taxonomy)
    second = classify_repository(observation, taxonomy)

    assert topics == original_topics
    assert first == second
    assert tuple(assertion.stable_json() for assertion in first) == tuple(
        assertion.stable_json() for assertion in second
    )


def test_classifier_strips_sentence_punctuation_from_positive_signals() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description="A modern API. A command-line.", topics=[], primary_language=None
    )

    assertions = classify_repository(observation, taxonomy)

    assert [assertion.capability_id for assertion in assertions] == ["api-backend", "cli"]


def test_classifier_strips_sentence_punctuation_from_exclusion_signals() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture(
        description="API client-only.", topics=[], primary_language=None
    )

    assertions = classify_repository(observation, taxonomy)

    assert assertions == ()


def test_rules_v2_matches_normalized_description_phrases_and_infers_parent() -> None:
    taxonomy = focused_taxonomy(
        [
            taxonomy_node("database-storage"),
            taxonomy_node("vector-database", parents=["database-storage"]),
        ],
        [
            {
                "capability_id": "vector-database",
                "description_phrases": ["vector database"],
            }
        ],
    )
    observation = repository_fixture(
        description="A fast vector-database for embeddings.",
        topics=[],
        primary_language="Rust",
    )

    assertions = classify_repository(observation, taxonomy)

    assert [assertion.capability_id for assertion in assertions] == [
        "database-storage",
        "vector-database",
    ]
    child = assertions[1]
    parent = assertions[0]
    assert Evidence(source="description", value="vector database") in child.evidence
    assert Evidence(source="taxonomy", value="derived-from:vector-database") in parent.evidence
    assert parent.confidence == child.confidence
    assert all(assertion.classifier_version == "rules-v2" for assertion in assertions)


def test_rules_v2_exclude_topics_veto_a_high_signal_child_rule() -> None:
    taxonomy = focused_taxonomy(
        [taxonomy_node("ai-ml"), taxonomy_node("ai-agent-framework", parents=["ai-ml"])],
        [
            {
                "capability_id": "ai-agent-framework",
                "topics": ["ai-agent"],
                "exclude_topics": ["awesome-list", "tutorials"],
            }
        ],
    )
    observation = repository_fixture(
        description="A curated collection of agent projects",
        topics=["ai-agent", "awesome-list"],
    )

    assert classify_repository(observation, taxonomy) == ()


def test_rules_v2_direct_parent_evidence_wins_over_inferred_evidence() -> None:
    taxonomy = focused_taxonomy(
        [taxonomy_node("cli"), taxonomy_node("terminal-ui", parents=["cli"])],
        [
            {"capability_id": "cli", "topics": ["cli"]},
            {"capability_id": "terminal-ui", "topics": ["tui"]},
        ],
    )
    observation = repository_fixture(description=None, topics=["cli", "tui"])

    assertions = classify_repository(observation, taxonomy)

    assert [assertion.capability_id for assertion in assertions] == ["cli", "terminal-ui"]
    cli = assertions[0]
    assert Evidence(source="topic", value="cli") in cli.evidence
    assert all(evidence.source != "taxonomy" for evidence in cli.evidence)


def test_rules_v2_closes_multiple_parent_paths_without_duplicates() -> None:
    taxonomy = focused_taxonomy(
        [
            taxonomy_node("ai-ml"),
            taxonomy_node("media"),
            taxonomy_node("computer-vision", parents=["ai-ml", "media"]),
        ],
        [{"capability_id": "computer-vision", "topics": ["ocr"]}],
    )
    observation = repository_fixture(description=None, topics=["ocr"])

    first = classify_repository(observation, taxonomy)
    second = classify_repository(observation, taxonomy)

    assert [assertion.capability_id for assertion in first] == [
        "ai-ml",
        "computer-vision",
        "media",
    ]
    assert first == second
    assert len({assertion.capability_id for assertion in first}) == len(first)
    assert all(
        Evidence(source="taxonomy", value="derived-from:computer-vision") in assertion.evidence
        for assertion in (first[0], first[2])
    )


def test_rules_v2_uses_stable_leaf_as_inherited_parent_provenance() -> None:
    taxonomy = focused_taxonomy(
        [
            taxonomy_node("testing"),
            taxonomy_node("api-testing", parents=["testing"]),
            taxonomy_node("browser-e2e-testing", parents=["testing"]),
        ],
        [
            {"capability_id": "api-testing", "topics": ["api-testing"]},
            {"capability_id": "browser-e2e-testing", "topics": ["browser-automation"]},
        ],
    )
    observation = repository_fixture(
        description=None,
        topics=["browser-automation", "api-testing"],
    )

    assertions = classify_repository(observation, taxonomy)

    parent = next(assertion for assertion in assertions if assertion.capability_id == "testing")
    assert Evidence(source="taxonomy", value="derived-from:api-testing") in parent.evidence
    assert (
        Evidence(source="taxonomy", value="derived-from:browser-e2e-testing") not in parent.evidence
    )
