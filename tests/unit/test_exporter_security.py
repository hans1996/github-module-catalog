"""Security-focused tests for deterministic catalog renderers."""

from __future__ import annotations

import importlib
import json

from test_exporters import _manifest, _ranked_manifest
from test_repository_publication import markdown_renderer, publisher

from github_module_catalog.exporters import (
    render_catalog_json,
    render_catalog_yaml,
    render_module_page,
    render_readme,
    render_taxonomy_page,
)
from github_module_catalog.models import CapabilityDefinition

publisher_validator = importlib.import_module(f"{publisher.__package__}.validation")


def test_taxonomy_markdown_escapes_html_and_uses_safe_metadata_code_spans() -> None:
    manifest = _manifest()
    definitions = tuple(
        CapabilityDefinition(
            id=definition.id,
            label=("<img src=x onerror=alert(1)>" if definition.id == "cli" else definition.label),
            parents=definition.parents,
        )
        for definition in manifest.capability_definitions
    )
    malicious = manifest.model_copy(
        update={
            "taxonomy_version": "2.0` <img src=x>",
            "classifier_version": "rules` <script>alert(1)</script>",
            "capability_definitions": definitions,
        }
    )

    markdown = render_taxonomy_page(malicious)

    assert "<img" not in markdown
    assert "<script" not in markdown
    assert "&lt;img src=x onerror=alert(1)&gt;" in markdown
    assert "Taxonomy version: `` 2.0` &lt;img src=x&gt; ``." in markdown
    assert "Classifier: `` rules` &lt;script&gt;alert(1)&lt;/script&gt; ``." in markdown


def test_license_uses_safe_html_escaped_code_span() -> None:
    manifest = _manifest()
    first = manifest.entries[0]
    repository = first.repository.model_copy(
        update={"license_spdx": "MIT` <img src=x onerror=alert(1)>"}
    )
    malicious = manifest.model_copy(
        update={"entries": (first.model_copy(update={"repository": repository}),)}
    )

    markdown = render_readme(malicious)

    assert "<img" not in markdown
    assert "`` MIT` &lt;img src=x onerror=alert(1)&gt; ``" in markdown


def test_yaml_is_canonical_pretty_json_and_yaml_1_2_compatible() -> None:
    manifest = _manifest()
    document = json.loads(render_catalog_json(manifest))
    expected = (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode()

    assert render_catalog_yaml(manifest) == expected


def test_exporter_and_stdlib_publisher_renderers_have_byte_parity() -> None:
    manifest = _ranked_manifest()
    document = json.loads(render_catalog_json(manifest))
    capability_ids = sorted(
        {assertion.capability_id for entry in manifest.entries for assertion in entry.assertions}
    )
    expected_markdown = {
        "README.md": render_readme(manifest).encode(),
        "taxonomy.md": render_taxonomy_page(manifest).encode(),
        **{
            f"modules/{capability_id}.md": render_module_page(
                manifest,
                capability_id,
            ).encode()
            for capability_id in capability_ids
        },
    }

    assert markdown_renderer.canonical_markdown_artifacts(document) == expected_markdown
    assert render_catalog_yaml(manifest) == publisher_validator._canonical_pretty_json(document)
