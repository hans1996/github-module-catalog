from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

import github_module_catalog.models as models
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
        "stargazers_count": 100,
        "observed_at": datetime(2024, 2, 2, tzinfo=UTC),
        "archived": False,
        "disabled": False,
        "fork": False,
        "private": False,
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


def capability_definition(
    capability_id: str,
    *,
    parents: tuple[str, ...] = (),
) -> models.CapabilityDefinition:
    return models.CapabilityDefinition(
        id=capability_id,
        label=capability_id.replace("-", " ").title(),
        parents=parents,
    )


def ranked_manifest(entries: tuple[CatalogEntry, ...], **overrides: Any) -> CatalogManifest:
    values: dict[str, Any] = {
        "schema_version": "1.0.0",
        "taxonomy_version": "1.0.0",
        "classifier_version": "rules-v1",
        "selection": models.CatalogSelectionCriteria(
            min_stars=100,
            pushed_since=datetime(2024, 1, 15, tzinfo=UTC),
            result_limit=1_000,
        ),
        "api_total_count": 2_500,
        "pages_fetched": 10,
        "result_limit": 1_000,
        "entries": entries,
    }
    return CatalogManifest(**(values | overrides))


def test_repository_observation_is_deeply_immutable() -> None:
    input_topics = ["Python", "CLI", "python"]
    observation = repository_fixture(topics=input_topics)

    with pytest.raises(ValidationError):
        observation.name = "changed"

    input_topics.append("security")
    assert observation.topics == ("cli", "python")
    with pytest.raises(TypeError):
        observation.topics[0] = "changed"  # type: ignore[index]


def test_catalog_selection_criteria_is_fixed_utc_and_immutable() -> None:
    criteria = models.CatalogSelectionCriteria(
        min_stars=100,
        pushed_since=datetime(2025, 7, 13, tzinfo=UTC),
        result_limit=1_000,
    )

    assert criteria.exclude_archived is True
    assert criteria.exclude_forks is True
    assert criteria.public_only is True
    assert criteria.sort == "stars"
    assert criteria.order == "desc"
    with pytest.raises(ValidationError):
        criteria.min_stars = 99


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"min_stars": -1}, "min_stars"),
        ({"pushed_since": datetime(2025, 7, 13)}, "pushed_since"),
        (
            {
                "pushed_since": datetime(
                    2025,
                    7,
                    13,
                    tzinfo=timezone(timedelta(hours=8)),
                )
            },
            "pushed_since",
        ),
        ({"exclude_archived": False}, "exclude_archived"),
        ({"exclude_archived": 1}, "exclude_archived"),
        ({"exclude_forks": False}, "exclude_forks"),
        ({"exclude_forks": 1}, "exclude_forks"),
        ({"public_only": False}, "public_only"),
        ({"public_only": 1}, "public_only"),
        ({"sort": "updated"}, "sort"),
        ({"order": "asc"}, "order"),
        ({"result_limit": 1_001}, "result_limit"),
    ],
)
def test_catalog_selection_criteria_rejects_weakened_or_unbounded_policy(
    overrides: dict[str, object], field_name: str
) -> None:
    values: dict[str, object] = {
        "min_stars": 100,
        "pushed_since": datetime(2025, 7, 13, tzinfo=UTC),
        "result_limit": 1_000,
    }

    with pytest.raises(ValidationError, match=field_name):
        models.CatalogSelectionCriteria.model_validate(values | overrides)


def test_repository_observation_accepts_a_nonnegative_strict_stargazer_count() -> None:
    observation = repository_fixture(stargazers_count=0)

    assert observation.stargazers_count == 0


def test_unknown_ranked_facts_preserve_legacy_observation_hash_contract() -> None:
    legacy = repository_fixture(stargazers_count=None, private=None)
    legacy_document = legacy.model_dump(mode="json", exclude_computed_fields=True)
    legacy_document.pop("stargazers_count")
    legacy_document.pop("private")
    expected_legacy_json = json.dumps(
        legacy_document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert legacy.stable_json() == expected_legacy_json

    ranked = repository_fixture(stargazers_count=100, private=False)
    assert '"private":false' in ranked.stable_json()
    assert '"stargazers_count":100' in ranked.stable_json()


@pytest.mark.parametrize("stargazers_count", [-1, True, "100"])
def test_repository_observation_rejects_invalid_stargazer_counts(
    stargazers_count: object,
) -> None:
    with pytest.raises(ValidationError, match="stargazers_count"):
        repository_fixture(stargazers_count=stargazers_count)


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
        {"html_url": "https://user:pass@github.com/octocat/hello-world"},
        {"html_url": "https://github.com/octocat/hello-world?download=1"},
        {"html_url": "https://github.com/octocat/hello-world#)![track](https://evil.test)"},
        {"html_url": "https://github.com:8443/octocat/hello-world"},
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


def test_repository_observation_accepts_legacy_owner_login_with_underscores() -> None:
    observation = repository_fixture(
        owner="up_the_irons",
        full_name="up_the_irons/hello-world",
        html_url="https://github.com/up_the_irons/hello-world",
    )

    assert observation.owner == "up_the_irons"


@pytest.mark.parametrize("owner", ["_leading", "bad owner", "bad/name", "bad!"])
def test_repository_observation_rejects_unsafe_owner_login_characters(owner: str) -> None:
    with pytest.raises(ValidationError, match="owner"):
        repository_fixture(
            owner=owner,
            full_name=f"{owner}/hello-world",
            html_url=f"https://github.com/{owner}/hello-world",
        )


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


def test_capability_definitions_are_immutable_and_canonical() -> None:
    child = capability_definition("terminal-ui", parents=("cli", "cli"))
    root = capability_definition("cli")

    manifest = CatalogManifest(
        schema_version="1.1.0",
        taxonomy_version="2.0.0",
        classifier_version="rules-v2",
        capability_definitions=(child, root),
    )

    assert [definition.id for definition in manifest.capability_definitions] == [
        "cli",
        "terminal-ui",
    ]
    assert manifest.capability_definitions[1].parents == ("cli",)
    with pytest.raises(ValidationError):
        manifest.capability_definitions[0].label = "Changed"


@pytest.mark.parametrize(
    ("definitions", "message"),
    [
        (
            (capability_definition("cli"), capability_definition("cli")),
            "duplicate capability definition",
        ),
        ((capability_definition("terminal-ui", parents=("cli",)),), "missing parent"),
        ((capability_definition("cli", parents=("cli",)),), "parent cycle"),
        (
            (
                capability_definition("first", parents=("second",)),
                capability_definition("second", parents=("first",)),
            ),
            "parent cycle",
        ),
    ],
)
def test_catalog_manifest_rejects_invalid_capability_hierarchy(
    definitions: tuple[models.CapabilityDefinition, ...],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        CatalogManifest(
            schema_version="1.1.0",
            taxonomy_version="2.0.0",
            classifier_version="rules-v2",
            capability_definitions=definitions,
        )


def test_catalog_manifest_rejects_assertion_outside_published_hierarchy() -> None:
    observation = repository_fixture()
    entry = CatalogEntry(
        repository=observation,
        assertions=(assertion_fixture(observation, capability_id="cli"),),
    )

    with pytest.raises(ValidationError, match="assertion targets missing capability definitions"):
        CatalogManifest(
            schema_version="1.1.0",
            taxonomy_version="2.0.0",
            classifier_version="rules-v2",
            capability_definitions=(capability_definition("auth"),),
            entries=(entry,),
        )


def test_ranked_manifest_canonicalizes_entries_by_contiguous_rank() -> None:
    lower_ranked_repository = repository_fixture(stargazers_count=100)
    higher_ranked_repository = repository_fixture(
        identity={"repository_id": 7},
        owner="example",
        name="service",
        full_name="example/service",
        html_url="https://github.com/example/service",
        stargazers_count=200,
    )
    lower_ranked_entry = CatalogEntry(repository=lower_ranked_repository, rank=2)
    higher_ranked_entry = CatalogEntry(repository=higher_ranked_repository, rank=1)

    manifest = ranked_manifest((lower_ranked_entry, higher_ranked_entry))
    reversed_manifest = ranked_manifest((higher_ranked_entry, lower_ranked_entry))

    assert [entry.rank for entry in manifest.entries] == [1, 2]
    assert [entry.repository.identity.repository_id for entry in manifest.entries] == [7, 42]
    assert manifest.stable_json() == reversed_manifest.stable_json()


@pytest.mark.parametrize(
    "repository_overrides",
    [
        {"stargazers_count": None},
        {"stargazers_count": 99},
        {"pushed_at": None},
        {"pushed_at": datetime(2024, 1, 10, tzinfo=UTC)},
        {"archived": None},
        {"archived": True},
        {"fork": None},
        {"fork": True},
        {"private": None},
        {"private": True},
    ],
)
def test_ranked_manifest_rejects_ineligible_repository_facts(
    repository_overrides: dict[str, object],
) -> None:
    entry = CatalogEntry(repository=repository_fixture(**repository_overrides), rank=1)

    with pytest.raises(ValidationError, match="ranked catalog entry"):
        ranked_manifest((entry,))


@pytest.mark.parametrize("ranks", [(1, 1), (1, 3)])
def test_ranked_manifest_rejects_duplicate_or_non_contiguous_ranks(
    ranks: tuple[int, int],
) -> None:
    first = repository_fixture(
        identity={"repository_id": 7},
        owner="example",
        name="service",
        full_name="example/service",
        html_url="https://github.com/example/service",
        stargazers_count=200,
    )
    second = repository_fixture(stargazers_count=100)

    with pytest.raises(ValidationError, match="unique contiguous ranks"):
        ranked_manifest(
            (
                CatalogEntry(repository=first, rank=ranks[0]),
                CatalogEntry(repository=second, rank=ranks[1]),
            )
        )


def test_ranked_manifest_rejects_rank_order_that_is_not_stars_desc_then_id() -> None:
    larger_id = repository_fixture(stargazers_count=200)
    smaller_id = repository_fixture(
        identity={"repository_id": 7},
        owner="example",
        name="service",
        full_name="example/service",
        html_url="https://github.com/example/service",
        stargazers_count=200,
    )

    with pytest.raises(ValidationError, match="stars descending"):
        ranked_manifest(
            (
                CatalogEntry(repository=larger_id, rank=1),
                CatalogEntry(repository=smaller_id, rank=2),
            )
        )


def test_ranked_manifest_requires_complete_search_coverage_metadata() -> None:
    entry = CatalogEntry(repository=repository_fixture(), rank=1)

    with pytest.raises(ValidationError, match="selection metadata"):
        CatalogManifest(
            schema_version="1.0.0",
            taxonomy_version="1.0.0",
            classifier_version="rules-v1",
            selection=models.CatalogSelectionCriteria(
                min_stars=100,
                pushed_since=datetime(2024, 1, 15, tzinfo=UTC),
                result_limit=1_000,
            ),
            entries=(entry,),
        )


def test_ranked_manifest_rejects_zero_fetched_pages_for_nonempty_entries() -> None:
    entry = CatalogEntry(repository=repository_fixture(), rank=1)

    with pytest.raises(ValidationError, match="pages_fetched"):
        ranked_manifest((entry,), pages_fetched=0)


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
