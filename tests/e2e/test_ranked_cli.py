"""End-to-end tests for ranked refresh and output-only validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import HttpUrl
from typer import Typer
from typer.testing import CliRunner

import github_module_catalog.cli as cli_module
import github_module_catalog.exporters as exporters
from github_module_catalog.cli import CliDependencies, create_app
from github_module_catalog.github_search import GitHubSearchError, RankedRepositorySnapshot
from github_module_catalog.models import (
    CatalogSelectionCriteria,
    RepositoryIdentity,
    RepositoryObservation,
)
from github_module_catalog.storage import RawObjectStore

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
RUNNER = CliRunner()


def _credential_marker() -> str:
    return "test-only-auth-marker"


def _search_item() -> dict[str, object]:
    return {
        "id": 7,
        "name": "module-catalog",
        "full_name": "octocat/module-catalog",
        "owner": {"login": "octocat", "id": 1},
        "html_url": "https://github.com/octocat/module-catalog",
        "description": "A reusable CLI catalog",
        "topics": ["cli"],
        "language": "Python",
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-11T00:00:00Z",
        "pushed_at": "2026-07-12T00:00:00Z",
        "stargazers_count": 500,
        "archived": False,
        "disabled": False,
        "fork": False,
        "private": False,
        "license": None,
    }


def _observation(*, observed_at: datetime = NOW) -> RepositoryObservation:
    return RepositoryObservation(
        identity=RepositoryIdentity(repository_id=7),
        owner="octocat",
        name="module-catalog",
        full_name="octocat/module-catalog",
        html_url=HttpUrl("https://github.com/octocat/module-catalog"),
        description="A reusable CLI catalog",
        topics=("cli",),
        primary_language="Python",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 11, tzinfo=UTC),
        pushed_at=datetime(2026, 7, 12, tzinfo=UTC),
        stargazers_count=500,
        observed_at=observed_at,
        archived=False,
        disabled=False,
        fork=False,
        private=False,
        license_spdx=None,
        license_name=None,
    )


@dataclass
class FakeRankedSource:
    received: list[tuple[CatalogSelectionCriteria, int]]
    fail: bool = False
    criteria_override: CatalogSelectionCriteria | None = None
    observed_at_override: datetime | None = None

    def collect_snapshot(
        self,
        criteria: CatalogSelectionCriteria,
        *,
        max_pages: int,
        raw_store: RawObjectStore,
    ) -> RankedRepositorySnapshot:
        self.received.append((criteria, max_pages))
        if self.fail:
            raise GitHubSearchError("simulated ranked source failure")
        raw_bytes = json.dumps(
            {
                "total_count": 1,
                "incomplete_results": False,
                "items": [_search_item()],
            },
            separators=(",", ":"),
        ).encode()
        raw_object = raw_store.write(raw_bytes)
        returned_criteria = self.criteria_override or criteria
        observed_at = self.observed_at_override or NOW
        return RankedRepositorySnapshot(
            criteria=returned_criteria,
            observations=(_observation(observed_at=observed_at),),
            repository_ranks=((7, 1),),
            api_total_count=1,
            pages_fetched=1,
            result_limit=returned_criteria.result_limit,
            raw_page_hashes=(raw_object.sha256,),
            observed_at=observed_at,
        )

    def close(self) -> None:
        return None


def _app(
    *,
    fail_on_calls: frozenset[int] = frozenset(),
    criteria_override: CatalogSelectionCriteria | None = None,
    observed_at_override: datetime | None = None,
) -> tuple[Typer, list[str], list[tuple[CatalogSelectionCriteria, int]]]:
    received_tokens: list[str] = []
    received: list[tuple[CatalogSelectionCriteria, int]] = []
    factory_calls = 0

    def ranked_source_factory(token: str) -> FakeRankedSource:
        nonlocal factory_calls
        factory_calls += 1
        received_tokens.append(token)
        return FakeRankedSource(
            received,
            fail=factory_calls in fail_on_calls,
            criteria_override=criteria_override,
            observed_at_override=observed_at_override,
        )

    dependencies = CliDependencies(
        ranked_source_factory=ranked_source_factory,
        now=lambda: NOW,
    )
    return create_app(dependencies), received_tokens, received


def _refresh_arguments(workspace: Path) -> list[str]:
    return [
        "refresh",
        "--workspace",
        str(workspace),
        "--min-stars",
        "100",
        "--active-within-days",
        "365",
        "--max-pages",
        "1",
    ]


def test_ranked_refresh_publishes_tracked_shape_and_validates_from_raw_evidence(
    tmp_path: Path,
) -> None:
    app, received_tokens, received = _app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    refreshed = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    (workspace / "data" / "state.sqlite3").unlink()
    validated = RUNNER.invoke(app, ["validate-output", "--workspace", str(workspace)])

    assert refreshed.exit_code == 0, refreshed.output
    assert validated.exit_code == 0, validated.output
    summary = json.loads(refreshed.stdout)
    assert summary["entries"] == 1
    assert summary["api_total_count"] == 1
    assert summary["pages_fetched"] == 1
    assert summary["result_limit"] == 100
    assert summary["classification_failures"] == 0
    assert _credential_marker() not in refreshed.output + validated.output
    assert received_tokens == [_credential_marker()]
    assert len(received) == 1
    criteria, max_pages = received[0]
    assert max_pages == 1
    assert criteria.min_stars == 100
    assert criteria.pushed_since == datetime(2025, 7, 13, tzinfo=UTC)
    assert criteria.result_limit == 100

    output = workspace / "catalog-output"
    assert {
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    } == {
        "README.md",
        "catalog.json",
        "catalog.yaml",
        "manifest.json",
        "modules/cli.md",
    }
    catalog = json.loads((output / "catalog.json").read_text())
    assert catalog["source"] == "github-search-repositories"
    assert catalog["selection"]["min_stars"] == 100
    assert catalog["selection"]["pushed_since"] == "2025-07-13T00:00:00Z"
    assert catalog["search_pages"] == [
        {
            "page_number": 1,
            "query": "stars:>=100 pushed:>=2025-07-13 archived:false is:public",
            "raw_sha256": catalog["raw_page_hashes"][0],
        }
    ]
    assert catalog["entries"][0]["rank"] == 1
    assert catalog["entries"][0]["repository"]["stargazers_count"] == 500


def test_failed_ranked_refresh_preserves_the_previous_complete_output(tmp_path: Path) -> None:
    app, _, _ = _app(fail_on_calls=frozenset({2}))
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0
    first = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    assert first.exit_code == 0, first.output
    output = workspace / "catalog-output"
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    failed = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    after = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    assert failed.exit_code != 0
    assert before == after
    assert _credential_marker() not in failed.output


def test_final_publication_failure_preserves_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = _app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0
    real_publish = exporters.publish_catalog
    publication_calls = 0

    def fail_fourth_publication(*args: object, **kwargs: object) -> tuple[Path, ...]:
        nonlocal publication_calls
        publication_calls += 1
        if publication_calls == 4:
            raise OSError("simulated final promotion failure")
        return real_publish(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_module, "publish_catalog", fail_fourth_publication)
    first = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    assert first.exit_code == 0, first.output
    output = workspace / "catalog-output"
    before = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    failed = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    after = {
        path.relative_to(output): path.read_bytes() for path in output.rglob("*") if path.is_file()
    }

    assert publication_calls == 4
    assert failed.exit_code != 0
    assert before == after
    assert not list(workspace.glob(".catalog-output.candidate-*"))


def test_ranked_refresh_rejects_forged_snapshot_criteria_before_publication(
    tmp_path: Path,
) -> None:
    forged = CatalogSelectionCriteria(
        min_stars=0,
        pushed_since=datetime(2020, 1, 1, tzinfo=UTC),
        result_limit=100,
    )
    app, _, _ = _app(criteria_override=forged)
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert result.exit_code != 0
    assert not (workspace / "catalog-output").exists()


def test_ranked_refresh_rejects_snapshot_time_after_trusted_command_clock(
    tmp_path: Path,
) -> None:
    app, _, _ = _app(observed_at_override=NOW + timedelta(days=1))
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert result.exit_code != 0
    assert not (workspace / "catalog-output").exists()


def test_ranked_refresh_defaults_to_the_top_thousand_one_year_window(tmp_path: Path) -> None:
    app, _, received = _app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        ["refresh", "--workspace", str(workspace)],
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert result.exit_code == 0, result.output
    criteria, max_pages = received[0]
    assert max_pages == 10
    assert criteria.min_stars == 100
    assert criteria.pushed_since == datetime(2025, 7, 13, tzinfo=UTC)
    assert criteria.result_limit == 1_000


def test_validate_output_rejects_missing_raw_provenance(tmp_path: Path) -> None:
    app, _, _ = _app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0
    refreshed = RUNNER.invoke(
        app,
        _refresh_arguments(workspace),
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    assert refreshed.exit_code == 0, refreshed.output
    catalog = json.loads((workspace / "catalog-output" / "catalog.json").read_text())
    raw_hash = catalog["raw_page_hashes"][0]
    raw_path = workspace / "data" / "raw" / "sha256" / raw_hash[:2] / f"{raw_hash}.json"
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == raw_hash
    raw_path.unlink()

    result = RUNNER.invoke(app, ["validate-output", "--workspace", str(workspace)])

    assert result.exit_code != 0


def test_ranked_refresh_requires_token_and_strict_bounded_options(tmp_path: Path) -> None:
    app, received_tokens, _ = _app()
    workspace = tmp_path / "workspace"
    assert RUNNER.invoke(app, ["init", "--workspace", str(workspace)]).exit_code == 0

    missing = RUNNER.invoke(app, _refresh_arguments(workspace))
    zero_days = RUNNER.invoke(
        app,
        [*_refresh_arguments(workspace)[:-4], "--active-within-days", "0", "--max-pages", "1"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )
    too_many_pages = RUNNER.invoke(
        app,
        [*_refresh_arguments(workspace)[:-2], "--max-pages", "11"],
        env={"GITHUB_TOKEN": _credential_marker()},
    )

    assert missing.exit_code != 0
    assert zero_days.exit_code != 0
    assert too_many_pages.exit_code != 0
    assert received_tokens == []
