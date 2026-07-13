from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from github_module_catalog.models import (
    CapabilityAssertion,
    CatalogEntry,
    CatalogManifest,
    Evidence,
    RepositoryIdentity,
    RepositoryObservation,
    ReuseStatus,
)


def repository_fixture(**overrides: Any) -> RepositoryObservation:
    values: dict[str, Any] = {
        "identity": {"repository_id": 42},
        "owner": "octocat",
        "name": "hello-world",
        "full_name": "octocat/hello-world",
        "html_url": "https://github.com/octocat/hello-world",
        "description": "A command-line API with authentication",
        "topics": ["Python", "CLI", "python"],
        "primary_language": "Python",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2024, 2, 1, tzinfo=UTC),
        "pushed_at": datetime(2024, 1, 31, tzinfo=UTC),
        "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        "archived": False,
        "disabled": False,
        "fork": False,
        "license_spdx": "MIT",
        "license_name": "MIT License",
    }
    return RepositoryObservation(**(values | overrides))


def assertion_fixture(observation: RepositoryObservation, **overrides: Any) -> CapabilityAssertion:
    values: dict[str, Any] = {
        "repository_id": observation.identity.repository_id,
        "capability_id": "cli",
        "taxonomy_version": "1.0.0",
        "classifier_version": "rules-v1",
        "confidence": 0.95,
        "evidence": [{"source": "topic", "value": "cli"}],
        "source_observation_hash": observation.stable_hash(),
        "license_spdx": observation.license_spdx,
        "reuse_status": observation.reuse_status,
    }
    return CapabilityAssertion(**(values | overrides))


def test_repository_observation_is_deeply_immutable() -> None:
    input_topics = ["Python", "CLI", "python"]
    observation = repository_fixture(topics=input_topics)

    with pytest.raises(ValidationError):
        observation.name = "changed"

    input_topics.append("security")
    assert observation.topics == ("cli", "python")
    with pytest.raises(TypeError):
        observation.topics[0] = "changed"  # type: ignore[index]


def test_models_reject_unknown_input_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RepositoryIdentity(repository_id=1, database_id=2)  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        repository_fixture(stars=100)


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"identity": {"repository_id": 0}}, "repository_id"),
        ({"html_url": "not-a-url"}, "html_url"),
        ({"created_at": datetime(2024, 1, 1)}, "created_at"),
        ({"topics": ["invalid topic"]}, "topics"),
        ({"license_spdx": "not a license!"}, "license_spdx"),
        ({"archived": "yes"}, "archived"),
    ],
)
def test_repository_observation_validates_source_facts(
    overrides: dict[str, object], field_name: str
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        repository_fixture(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"html_url": "https://github.com/other/repository"},
        {"observed_at": datetime(2024, 1, 15, tzinfo=UTC)},
        {"pushed_at": datetime(2024, 3, 1, tzinfo=UTC)},
    ],
)
def test_repository_observation_rejects_mismatched_url_or_future_source_facts(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="html_url|observed_at"):
        repository_fixture(**overrides)


def test_repository_observation_allows_normalized_trailing_url_slash() -> None:
    observation = repository_fixture(
        html_url="https://github.com/octocat/hello-world/",
    )

    assert observation.identity.repository_id == 42


def test_repository_identity_is_the_numeric_github_id() -> None:
    before_rename = RepositoryIdentity(repository_id=42)
    after_rename = RepositoryIdentity(repository_id=42)

    assert before_rename == after_rename
    assert hash(before_rename) == hash(after_rename)


@pytest.mark.parametrize("license_spdx", [None, "NOASSERTION", "GPL-3.0-only"])
def test_unlicensed_or_non_permissive_repository_is_discovery_only(
    license_spdx: str | None,
) -> None:
    observation = repository_fixture(license_spdx=license_spdx)

    assert observation.reuse_status == ReuseStatus.DISCOVERY_ONLY


def test_permissive_license_is_only_a_reuse_signal_for_active_repositories() -> None:
    active = repository_fixture(license_spdx="Apache-2.0")
    archived = repository_fixture(license_spdx="MIT", archived=True)
    disabled = repository_fixture(license_spdx="BSD-3-Clause", disabled=True)

    assert active.reuse_status == ReuseStatus.SAFE_TO_INTEGRATE
    assert archived.reuse_status == ReuseStatus.DISCOVERY_ONLY
    assert disabled.reuse_status == ReuseStatus.DISCOVERY_ONLY


@pytest.mark.parametrize(
    ("archived", "disabled"),
    [(None, None), (None, False), (False, None)],
)
def test_permissive_license_with_unknown_lifecycle_is_discovery_only(
    archived: bool | None, disabled: bool | None
) -> None:
    observation = repository_fixture(
        license_spdx="MIT",
        archived=archived,
        disabled=disabled,
    )

    assert observation.reuse_status == ReuseStatus.DISCOVERY_ONLY


def test_sparse_observation_serializes_unknown_facts_deterministically() -> None:
    first = repository_fixture(
        created_at=None,
        updated_at=None,
        pushed_at=None,
        archived=None,
        disabled=None,
        fork=None,
        topics=[],
        primary_language=None,
        license_spdx=None,
        license_name=None,
    )
    second = RepositoryObservation.model_validate_json(first.stable_json())

    assert first == second
    assert first.stable_hash() == second.stable_hash()
    assert '"archived":null' in first.stable_json()
    assert '"created_at":null' in first.stable_json()
    assert first.reuse_status == ReuseStatus.DISCOVERY_ONLY


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "created_at": datetime(2024, 3, 1, tzinfo=UTC),
            "updated_at": None,
            "pushed_at": None,
            "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        },
        {
            "created_at": None,
            "updated_at": datetime(2024, 3, 1, tzinfo=UTC),
            "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        },
    ],
)
def test_sparse_observation_rejects_future_timestamp_when_comparable(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="observed_at"):
        repository_fixture(**overrides)


def test_observation_has_byte_stable_serialization_and_hashing() -> None:
    first = repository_fixture(topics=["python", "cli"])
    second = repository_fixture(topics=["CLI", "Python", "cli"])

    assert first.stable_json() == second.stable_json()
    assert first.stable_hash() == second.stable_hash()
    assert first.stable_json().startswith('{"archived":false,')


def test_capability_assertion_validates_provenance_and_confidence() -> None:
    observation = repository_fixture()
    assertion = assertion_fixture(observation)

    assert assertion.evidence == (Evidence(source="topic", value="cli"),)
    with pytest.raises(ValidationError, match="confidence"):
        assertion_fixture(observation, confidence=1.01)
    with pytest.raises(ValidationError, match="evidence"):
        assertion_fixture(observation, evidence=[])
    with pytest.raises(ValidationError, match="source_observation_hash"):
        assertion_fixture(observation, source_observation_hash="not-a-hash")
    with pytest.raises(ValidationError, match="license_spdx"):
        assertion_fixture(observation, license_spdx="not a license!")


def test_catalog_entry_rejects_assertions_from_another_observation() -> None:
    observation = repository_fixture()
    assertion = assertion_fixture(observation, repository_id=999)

    with pytest.raises(ValidationError, match="repository_id"):
        CatalogEntry(repository=observation, assertions=(assertion,))


@pytest.mark.parametrize(
    "forged_fields",
    [
        {"license_spdx": "MIT", "reuse_status": ReuseStatus.SAFE_TO_INTEGRATE},
        {"license_spdx": None, "reuse_status": ReuseStatus.SAFE_TO_INTEGRATE},
    ],
)
def test_catalog_entry_rejects_forged_reuse_metadata(
    forged_fields: dict[str, object],
) -> None:
    observation = repository_fixture(license_spdx=None, license_name=None)
    assertion = assertion_fixture(observation, **forged_fields)

    with pytest.raises(ValidationError, match="license_spdx|reuse_status"):
        CatalogEntry(repository=observation, assertions=(assertion,))


def test_catalog_entry_rejects_duplicate_capability_ids() -> None:
    observation = repository_fixture()
    first = assertion_fixture(observation, confidence=0.9)
    second = assertion_fixture(observation, confidence=0.8)

    with pytest.raises(ValidationError, match="duplicate capability_id"):
        CatalogEntry(repository=observation, assertions=(first, second))


def test_catalog_manifest_rejects_duplicate_repository_ids() -> None:
    first_repository = repository_fixture(description="First observation")
    second_repository = repository_fixture(description="Second observation")
    first_entry = CatalogEntry(repository=first_repository)
    second_entry = CatalogEntry(repository=second_repository)

    with pytest.raises(ValidationError, match="duplicate repository_id"):
        CatalogManifest(
            schema_version="1.0.0",
            taxonomy_version="1.0.0",
            classifier_version="rules-v1",
            generated_at=datetime(2024, 2, 2, tzinfo=UTC),
            entries=(first_entry, second_entry),
        )


def test_catalog_manifest_canonicalizes_entry_and_assertion_order() -> None:
    first_repository = repository_fixture()
    second_repository = repository_fixture(
        identity={"repository_id": 7},
        owner="example",
        name="service",
        full_name="example/service",
        html_url="https://github.com/example/service",
    )
    cli = assertion_fixture(first_repository, capability_id="cli")
    auth = assertion_fixture(first_repository, capability_id="auth")
    first_entry = CatalogEntry(repository=first_repository, assertions=(cli, auth))
    second_entry = CatalogEntry(repository=second_repository, assertions=())

    manifest = CatalogManifest(
        schema_version="1.0.0",
        taxonomy_version="1.0.0",
        classifier_version="rules-v1",
        generated_at=datetime(2024, 2, 2, tzinfo=UTC),
        entries=(first_entry, second_entry),
    )
    reversed_manifest = CatalogManifest(
        schema_version="1.0.0",
        taxonomy_version="1.0.0",
        classifier_version="rules-v1",
        generated_at=datetime(2024, 2, 2, tzinfo=UTC),
        entries=(second_entry, first_entry),
    )

    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [7, 42]
    assert [assertion.capability_id for assertion in manifest.entries[1].assertions] == [
        "auth",
        "cli",
    ]
    assert manifest.entry_count == 2
    assert manifest.stable_json() == reversed_manifest.stable_json()
