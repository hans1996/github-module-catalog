"""Tests for validated catalog promotion into the tracked repository tree."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

BEGIN = "<!-- catalog-index:begin -->"
END = "<!-- catalog-index:end -->"
PACKAGE_PATH = Path(__file__).parents[2] / "scripts" / "catalog_publisher"
SPEC = importlib.util.spec_from_file_location(
    "repository_catalog_publisher",
    PACKAGE_PATH / "__init__.py",
    submodule_search_locations=[str(PACKAGE_PATH)],
)
assert SPEC is not None and SPEC.loader is not None
package = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = package
SPEC.loader.exec_module(package)
publisher: Any = importlib.import_module(f"{SPEC.name}.publication")


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _repository_document(
    repository_id: int,
    *,
    name: str,
    stars: int,
    description: str,
) -> dict[str, object]:
    return {
        "identity": {"repository_id": repository_id},
        "owner": "example",
        "name": name,
        "full_name": f"example/{name}",
        "html_url": f"https://github.com/example/{name}",
        "description": description,
        "topics": [],
        "pushed_at": "2026-07-01T00:00:00Z",
        "stargazers_count": stars,
        "observed_at": "2026-07-13T12:00:00Z",
        "archived": False,
        "disabled": False,
        "fork": False,
        "private": False,
    }


def _catalog_document() -> dict[str, Any]:
    raw_hash = "a" * 64
    return {
        "schema_version": "1.0.0",
        "taxonomy_version": "1.0.0",
        "classifier_version": "rules-v1",
        "generated_at": "2026-07-13T12:00:00Z",
        "source": "github-search-repositories",
        "selection": {
            "min_stars": 100,
            "pushed_since": "2025-07-13T00:00:00Z",
            "exclude_archived": True,
            "exclude_forks": True,
            "public_only": True,
            "sort": "stars",
            "order": "desc",
            "result_limit": 2,
        },
        "api_total_count": 2_500,
        "pages_fetched": 1,
        "result_limit": 2,
        "search_pages": [
            {
                "page_number": 1,
                "query": "stars:>=100 pushed:>=2025-07-13 archived:false is:public",
                "raw_sha256": raw_hash,
            }
        ],
        "cursor_start": 0,
        "cursor_end": 0,
        "discovered_count": 2,
        "validated_observation_count": 2,
        "pending_count": 0,
        "retry_count": 0,
        "dead_letter_count": 0,
        "source_hashes": ["b" * 64, "c" * 64],
        "raw_page_hashes": [raw_hash],
        "classification_failure_repository_ids": [],
        "coverage_complete": False,
        "coverage_note": "<img src=x onerror=alert(1)> [bad](javascript:alert(1))",
        "entries": [
            {
                "rank": 1,
                "repository": _repository_document(
                    7,
                    name="one",
                    stars=500,
                    description="<script>alert(1)</script>",
                ),
                "assertions": [{"capability_id": "cli"}, {"capability_id": "ai-ml"}],
            },
            {
                "rank": 2,
                "repository": _repository_document(
                    8,
                    name="two",
                    stars=400,
                    description="![x](javascript:bad)",
                ),
                "assertions": [{"capability_id": "cli"}],
            },
        ],
        "entry_count": 2,
        "capability_count": 2,
    }


def _write_catalog_source(root: Path, catalog: dict[str, Any]) -> Path:
    source = root / "catalog-output"
    (source / "modules").mkdir(parents=True)
    artifacts: dict[str, bytes] = {
        "README.md": b"# Full ranked catalog\n",
        "catalog.json": _canonical_json(catalog),
        "catalog.yaml": b"source: github-search-repositories\n",
        "modules/ai-ml.md": b"# ai-ml\n",
        "modules/cli.md": b"# cli\n",
    }
    for name, content in artifacts.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {key: value for key, value in catalog.items() if key != "entries"}
    manifest["artifacts"] = {
        name: hashlib.sha256(content).hexdigest() for name, content in artifacts.items()
    }
    (source / "manifest.json").write_bytes(_canonical_json(manifest))
    return source


def _write_source(root: Path, *, catalog_updates: dict[str, object] | None = None) -> Path:
    catalog = _catalog_document()
    if catalog_updates:
        catalog = {**catalog, **catalog_updates}
    return _write_catalog_source(root, catalog)


def _repository(root: Path, *, readme: bytes | None = None) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "README.md").write_bytes(
        readme
        or (
            b"# Manual heading\r\n\r\n"
            + BEGIN.encode()
            + b"\r\nold generated bytes\r\n"
            + END.encode()
            + b"\r\n\r\nManual footer\r\n"
        )
    )
    return repository


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_publish_updates_homepage_and_complete_catalog_idempotently(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    stale = repository / "catalog" / "modules" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    manual_prefix = b"# Manual heading\r\n\r\n" + BEGIN.encode() + b"\r\n"
    manual_suffix = END.encode() + b"\r\n\r\nManual footer\r\n"

    first = publisher.publish_to_repository(source, repository)
    first_bytes = _tree_bytes(repository)
    second = publisher.publish_to_repository(source, repository)
    second_bytes = _tree_bytes(repository)

    assert first == second
    assert first.entries == 2
    assert first.capabilities == 2
    assert first.catalog_files == 6
    assert first_bytes == second_bytes
    assert not stale.exists()
    assert set(first_bytes) == {
        Path("README.md"),
        Path("catalog/README.md"),
        Path("catalog/catalog.json"),
        Path("catalog/catalog.yaml"),
        Path("catalog/manifest.json"),
        Path("catalog/modules/ai-ml.md"),
        Path("catalog/modules/cli.md"),
    }
    readme = (repository / "README.md").read_bytes()
    assert readme.startswith(manual_prefix)
    assert readme.endswith(manual_suffix)
    rendered = readme.decode()
    assert "Minimum stars: **100**" in rendered
    assert "2 unique repositories" in rendered
    assert "2,500 GitHub matches" in rendered
    assert "[Full ranked catalog](catalog/README.md)" in rendered
    assert "[`ai-ml`](catalog/modules/ai-ml.md) — 1" in rendered
    assert "[`cli`](catalog/modules/cli.md) — 2" in rendered
    assert "<script>" not in rendered
    assert "javascript:" not in rendered
    assert "<img" not in rendered


@pytest.mark.parametrize("failure", ["digest", "extra", "missing"])
def test_publish_rejects_manifest_or_file_set_mismatch_before_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)
    if failure == "digest":
        (source / "catalog.json").write_bytes(b"{}\n")
    elif failure == "extra":
        (source / "unexpected.txt").write_text("unexpected")
    else:
        (source / "catalog.yaml").unlink()

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_traversal_and_symlinked_source_files(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"]["../README.md"] = "0" * 64
    manifest_path.write_bytes(_canonical_json(manifest))
    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    source = _write_source(tmp_path / "second")
    target = tmp_path / "outside.md"
    target.write_text("outside")
    (source / "README.md").unlink()
    (source / "README.md").symlink_to(target)
    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)
    assert target.read_text() == "outside"


@pytest.mark.parametrize(
    ("repository_field", "invalid_value"),
    [
        ("stargazers_count", 99),
        ("pushed_at", "2025-07-12T23:59:59Z"),
        ("archived", True),
        ("fork", True),
        ("private", True),
    ],
)
def test_publish_rejects_repository_outside_ranked_selection_before_mutation(
    tmp_path: Path,
    repository_field: str,
    invalid_value: object,
) -> None:
    catalog = _catalog_document()
    first, second = catalog["entries"]
    invalid_first = {
        **first,
        "repository": {
            **first["repository"],
            repository_field: invalid_value,
        },
    }
    source = _write_catalog_source(
        tmp_path,
        {**catalog, "entries": [invalid_first, second]},
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize("failure", ["rank", "duplicate_id", "rank_order"])
def test_publish_rejects_invalid_rank_identity_or_order_before_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    catalog = _catalog_document()
    first, second = catalog["entries"]
    if failure == "rank":
        entries = [{**first, "rank": 2}, second]
    elif failure == "duplicate_id":
        duplicate_repository = {
            **second["repository"],
            "identity": {"repository_id": first["repository"]["identity"]["repository_id"]},
        }
        entries = [first, {**second, "repository": duplicate_repository}]
    else:
        lower_first = {
            **first,
            "repository": {**first["repository"], "stargazers_count": 400},
        }
        higher_second = {
            **second,
            "repository": {**second["repository"], "stargazers_count": 500},
        }
        entries = [lower_first, higher_second]
    source = _write_catalog_source(tmp_path, {**catalog, "entries": entries})
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize(
    "failure",
    ["api_total", "page_count", "page_query", "raw_page_hashes"],
)
def test_publish_rejects_inconsistent_ranked_coverage_before_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    catalog = _catalog_document()
    if failure == "api_total":
        invalid = {**catalog, "api_total_count": 1}
    elif failure == "page_count":
        invalid = {**catalog, "pages_fetched": 2}
    elif failure == "page_query":
        page = {**catalog["search_pages"][0], "query": "stars:>=0"}
        invalid = {**catalog, "search_pages": [page]}
    else:
        invalid = {**catalog, "raw_page_hashes": ["d" * 64]}
    source = _write_catalog_source(tmp_path, invalid)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize("field", ["generated_at", "pushed_since"])
def test_publish_rejects_noncanonical_timestamp_separator_before_mutation(
    tmp_path: Path,
    field: str,
) -> None:
    catalog = _catalog_document()
    malicious_timestamp = (
        "2026-07-13\n12:00:00Z" if field == "generated_at" else "2025-07-13\n00:00:00Z"
    )
    if field == "generated_at":
        invalid = {**catalog, "generated_at": malicious_timestamp}
    else:
        invalid = {
            **catalog,
            "selection": {
                **catalog["selection"],
                "pushed_since": malicious_timestamp,
            },
        }
    source = _write_catalog_source(tmp_path, invalid)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize(
    "readme",
    [
        b"no markers\n",
        (BEGIN + "\n" + BEGIN + "\n" + END + "\n").encode(),
        (END + "\n" + BEGIN + "\n").encode(),
        ("prefix " + BEGIN + " suffix\n" + END + "\n").encode(),
    ],
)
def test_publish_rejects_missing_duplicate_reversed_or_embedded_markers(
    tmp_path: Path,
    readme: bytes,
) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path, readme=readme)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_transaction_rolls_back_catalog_and_readme_if_final_rename_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = _write_source(tmp_path / "first")
    repository = _repository(tmp_path)
    publisher.publish_to_repository(first_source, repository)
    before = _tree_bytes(repository)
    second_source = _write_source(
        tmp_path / "second",
        catalog_updates={"generated_at": "2026-07-14T12:00:00Z"},
    )
    real_replace = publisher.os.replace

    def fail_readme_install(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if str(source).startswith(".README.md.stage-") and destination == "README.md":
            raise OSError("simulated README install failure")
        real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(publisher.os, "replace", fail_readme_install)

    with pytest.raises(OSError, match="simulated"):
        publisher.publish_to_repository(second_source, repository)

    assert _tree_bytes(repository) == before
    assert not list(repository.glob(".catalog-*"))
    assert not list(repository.glob(".README.md.*"))


def test_incomplete_rollback_preserves_old_readme_backup_and_restores_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_source = _write_source(tmp_path / "first")
    repository = _repository(tmp_path)
    publisher.publish_to_repository(first_source, repository)
    readme_before = (repository / "README.md").read_bytes()
    catalog_before = _tree_bytes(repository / "catalog")
    second_source = _write_source(
        tmp_path / "second",
        catalog_updates={"generated_at": "2026-07-14T12:00:00Z"},
    )
    repository_details = repository.stat()
    real_fsync = publisher.os.fsync
    real_rename = publisher.os.rename
    real_replace = publisher.os.replace
    commit_failure_raised = False

    def fail_commit_fsync(descriptor: int) -> None:
        nonlocal commit_failure_raised
        details = publisher.os.fstat(descriptor)
        is_repository = (
            details.st_dev == repository_details.st_dev
            and details.st_ino == repository_details.st_ino
        )
        new_readme_is_installed = (repository / "README.md").read_bytes() != readme_before
        if is_repository and new_readme_is_installed and not commit_failure_raised:
            commit_failure_raised = True
            raise OSError("simulated commit durability failure")
        real_fsync(descriptor)

    def fail_restore_with(
        operation: Any,
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        if str(source).startswith(".README.md.backup-") and destination == "README.md":
            raise OSError("simulated README restore failure")
        operation(source, destination, *args, **kwargs)

    def guarded_rename(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        fail_restore_with(real_rename, source, destination, *args, **kwargs)

    def guarded_replace(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        fail_restore_with(real_replace, source, destination, *args, **kwargs)

    monkeypatch.setattr(publisher.os, "fsync", fail_commit_fsync)
    monkeypatch.setattr(publisher.os, "rename", guarded_rename)
    monkeypatch.setattr(publisher.os, "replace", guarded_replace)

    with pytest.raises(OSError, match="simulated commit durability"):
        publisher.publish_to_repository(second_source, repository)

    assert (repository / "README.md").is_file()
    assert _tree_bytes(repository / "catalog") == catalog_before
    readme_backups = list(repository.glob(".README.md.backup-*"))
    assert len(readme_backups) == 1
    assert readme_backups[0].read_bytes() == readme_before
    assert not list(repository.glob(".catalog-backup-*"))


def test_publish_revalidates_staged_catalog_bytes_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)
    real_promote = publisher._promote_transaction

    def tamper_then_promote(repository_fd: int, **kwargs: object) -> None:
        stage_name = kwargs["stage_name"]
        assert isinstance(stage_name, str)
        (repository / stage_name / "catalog.json").write_bytes(b"{}\n")
        real_promote(repository_fd, **kwargs)

    monkeypatch.setattr(publisher, "_promote_transaction", tamper_then_promote)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_replaced_readme_temp_without_deleting_foreign_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)
    real_promote = publisher._promote_transaction

    def replace_temp_then_promote(repository_fd: int, **kwargs: object) -> None:
        readme_temp = kwargs["readme_temp"]
        assert isinstance(readme_temp, str)
        temp_path = repository / readme_temp
        temp_path.unlink()
        temp_path.write_bytes(b"foreign bytes\n")
        real_promote(repository_fd, **kwargs)

    monkeypatch.setattr(publisher, "_promote_transaction", replace_temp_then_promote)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) != before
    assert (repository / "README.md").read_bytes() == before[Path("README.md")]
    foreign_temps = list(repository.glob(".README.md.stage-*"))
    assert len(foreign_temps) == 1
    assert foreign_temps[0].read_bytes() == b"foreign bytes\n"
    assert not (repository / "catalog").exists()


def test_publish_detects_in_place_readme_change_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(tmp_path)
    repository = _repository(tmp_path)
    real_promote = publisher._promote_transaction

    def modify_readme_then_promote(repository_fd: int, **kwargs: object) -> None:
        readme = repository / "README.md"
        observed = readme.read_bytes()
        readme.write_bytes(b"!" + observed[1:])
        real_promote(repository_fd, **kwargs)

    monkeypatch.setattr(publisher, "_promote_transaction", modify_readme_then_promote)

    with pytest.raises(publisher.PublicationError):
        publisher.publish_to_repository(source, repository)

    assert not (repository / "catalog").exists()
    assert b"Ranked catalog index" not in (repository / "README.md").read_bytes()
