"""Security limits and content-binding tests for repository publication."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest
from test_repository_publication import (
    _canonical_json,
    _canonical_pretty_json,
    _catalog_document,
    _repository,
    _tree_bytes,
    _write_catalog_source,
    _write_source,
    publisher,
)

validator = importlib.import_module(f"{publisher.__package__}.validation")


def _replace_artifact_and_resign(source: Path, name: str, content: bytes) -> None:
    artifact = source / name
    artifact.write_bytes(content)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][name] = hashlib.sha256(content).hexdigest()
    manifest_path.write_bytes(_canonical_json(manifest))


@pytest.mark.parametrize("artifact", ["README.md", "taxonomy.md", "modules/cli.md"])
def test_publish_rejects_resigned_noncanonical_markdown_before_mutation(
    tmp_path: Path,
    artifact: str,
) -> None:
    source = _write_source(tmp_path)
    malicious = b"# attacker-controlled markdown\n<script>alert(1)</script>\n"
    _replace_artifact_and_resign(source, artifact, malicious)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="canonical Markdown"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_canonical_markdown_escapes_html_and_uses_safe_code_spans(
    tmp_path: Path,
) -> None:
    catalog = _catalog_document()
    definitions = [
        {
            **definition,
            "label": (
                "<img src=x onerror=alert(1)>" if definition["id"] == "cli" else definition["label"]
            ),
        }
        for definition in catalog["capability_definitions"]
    ]
    first, second = catalog["entries"]
    first_repository = {
        **first["repository"],
        "license_spdx": "MIT` <img src=x onerror=alert(1)>",
    }
    malicious = {
        **catalog,
        "taxonomy_version": "2.0` <img src=x>",
        "classifier_version": "rules` <script>alert(1)</script>",
        "capability_definitions": definitions,
        "entries": [{**first, "repository": first_repository}, second],
    }
    source = _write_catalog_source(tmp_path, malicious)
    repository = _repository(tmp_path)

    publisher.publish_to_repository(source, repository)

    taxonomy = (repository / "catalog" / "taxonomy.md").read_text()
    readme = (repository / "catalog" / "README.md").read_text()
    assert "<img" not in taxonomy
    assert "<script" not in taxonomy
    assert "&lt;img src=x onerror=alert(1)&gt;" in taxonomy
    assert "Taxonomy version: `` 2.0` &lt;img src=x&gt; ``." in taxonomy
    assert "Classifier: `` rules` &lt;script&gt;alert(1)&lt;/script&gt; ``." in taxonomy
    assert "<img" not in readme
    assert "`` MIT` &lt;img src=x onerror=alert(1)&gt; ``" in readme


def test_publish_rejects_resigned_noncanonical_yaml_before_mutation(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    _replace_artifact_and_resign(
        source,
        "catalog.yaml",
        b"source: github-search-repositories\n",
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="canonical YAML"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize("artifact", ["manifest.json", "catalog.json"])
def test_publish_rejects_noncanonical_json_bytes_before_mutation(
    tmp_path: Path,
    artifact: str,
) -> None:
    source = _write_source(tmp_path)
    path = source / artifact
    pretty = _canonical_pretty_json(json.loads(path.read_text()))
    if artifact == "catalog.json":
        _replace_artifact_and_resign(source, artifact, pretty)
    else:
        path.write_bytes(pretty)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="canonical JSON"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_duplicate_catalog_key_even_when_resigned(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    catalog_path = source / "catalog.json"
    duplicate = b'{"source":"ignored",' + catalog_path.read_bytes()[1:]
    _replace_artifact_and_resign(source, "catalog.json", duplicate)
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="duplicate"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


@pytest.mark.parametrize("payload", [b'{"key":1,"key":2}', b'{"key":NaN}'])
def test_publisher_json_parser_rejects_duplicate_keys_and_nonfinite_constants(
    payload: bytes,
) -> None:
    with pytest.raises(publisher.PublicationError, match="valid UTF-8 JSON"):
        validator._json_object(payload, "test JSON")


def test_publish_rejects_capability_with_too_many_parents_before_mutation(
    tmp_path: Path,
) -> None:
    catalog = _catalog_document()
    extra = [
        {"id": f"parent-{index}", "label": f"Parent {index}", "parents": []} for index in range(5)
    ]
    definitions = [
        *catalog["capability_definitions"],
        *extra,
        {
            "id": "wide-child",
            "label": "Wide child",
            "parents": [definition["id"] for definition in extra],
        },
    ]
    source = _write_source(
        tmp_path,
        catalog_updates={
            "capability_definitions": sorted(definitions, key=lambda item: item["id"])
        },
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="capability hierarchy"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_capability_hierarchy_over_depth_limit_before_mutation(
    tmp_path: Path,
) -> None:
    catalog = _catalog_document()
    chain = [
        {
            "id": f"depth-{index:02d}",
            "label": f"Depth {index}",
            "parents": [] if index == 0 else [f"depth-{index - 1:02d}"],
        }
        for index in range(18)
    ]
    definitions = [*catalog["capability_definitions"], *chain]
    source = _write_source(
        tmp_path,
        catalog_updates={
            "capability_definitions": sorted(definitions, key=lambda item: item["id"])
        },
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="capability hierarchy"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_capability_hierarchy_over_definition_limit_before_mutation(
    tmp_path: Path,
) -> None:
    catalog = _catalog_document()
    definitions = [
        *catalog["capability_definitions"],
        *(
            {"id": f"unused-{index:03d}", "label": f"Unused {index}", "parents": []}
            for index in range(254)
        ),
    ]
    source = _write_source(
        tmp_path,
        catalog_updates={
            "capability_definitions": sorted(definitions, key=lambda item: item["id"])
        },
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="capability hierarchy"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before


def test_publish_rejects_capability_hierarchy_over_edge_limit_before_mutation(
    tmp_path: Path,
) -> None:
    catalog = _catalog_document()
    parents = [
        {"id": f"shared-parent-{index}", "label": f"Shared parent {index}", "parents": []}
        for index in range(4)
    ]
    children = [
        {
            "id": f"wide-node-{index:03d}",
            "label": f"Wide node {index}",
            "parents": [definition["id"] for definition in parents],
        }
        for index in range(129)
    ]
    definitions = [*catalog["capability_definitions"], *parents, *children]
    source = _write_source(
        tmp_path,
        catalog_updates={
            "capability_definitions": sorted(definitions, key=lambda item: item["id"])
        },
    )
    repository = _repository(tmp_path)
    before = _tree_bytes(repository)

    with pytest.raises(publisher.PublicationError, match="capability hierarchy"):
        publisher.publish_to_repository(source, repository)

    assert _tree_bytes(repository) == before
