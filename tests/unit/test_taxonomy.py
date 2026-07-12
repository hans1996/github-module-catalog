from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from github_module_catalog.models import RepositoryObservation, ReuseStatus
from github_module_catalog.taxonomy import classify_repository, load_taxonomy

TAXONOMY_PATH = Path(__file__).parents[2] / "config" / "taxonomy.yaml"
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

    assert taxonomy.version == "1.0.0"
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


def test_taxonomy_rejects_unknown_configuration_fields(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("version: 1.0.0\naxes: {}\nrules: []\nunexpected: true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_taxonomy(invalid)


def test_classifier_returns_deterministic_multi_label_assertions_with_provenance() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    observation = repository_fixture()

    assertions = classify_repository(observation, taxonomy, classifier_version="rules-v1")

    assert [assertion.capability_id for assertion in assertions] == ["api-backend", "auth", "cli"]
    assert all(assertion.repository_id == 42 for assertion in assertions)
    assert all(assertion.taxonomy_version == "1.0.0" for assertion in assertions)
    assert all(assertion.classifier_version == "rules-v1" for assertion in assertions)
    assert all(
        assertion.source_observation_hash == observation.stable_hash() for assertion in assertions
    )
    assert all(0.0 <= assertion.confidence <= 1.0 for assertion in assertions)
    assert all(assertion.evidence for assertion in assertions)
    assert all(assertion.reuse_status == ReuseStatus.SAFE_TO_INTEGRATE for assertion in assertions)
    assert any(
        evidence.source == "language" for assertion in assertions for evidence in assertion.evidence
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
