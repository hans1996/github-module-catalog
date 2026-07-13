"""Read-only assertions for pinned least-privilege workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).parents[2]
NODE24_ACTIONS = {
    "actions/checkout": {
        "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
    },
    "actions/cache/restore": {"27d5ce7f107fe9357f9df03efb73ab90386fccae"},
    "actions/cache/save": {"27d5ce7f107fe9357f9df03efb73ab90386fccae"},
    "actions/upload-artifact": {"043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"},
    "actions/download-artifact": {"3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"},
    "astral-sh/setup-uv": {
        "cec208311dfd045dd5311c1add060b2062131d57",
        "11f9893b081a58869d3b5fccaea48c9e9e46f990",
    },
}


def _discovery_workflow() -> dict[str, Any]:
    path = ROOT / ".github" / "workflows" / "discover.yml"
    return cast(dict[str, Any], yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader))


def test_workflows_use_reviewed_full_node24_action_pins() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )
    observed = re.findall(r"uses:\s+([^@\s]+)@([0-9a-f]{40})", workflows)

    assert observed
    for action, commit in observed:
        assert commit in NODE24_ACTIONS[action]


def test_discovery_defaults_to_popular_repositories_active_within_one_year() -> None:
    workflow = _discovery_workflow()
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"min_stars", "active_within_days", "max_pages"}
    assert inputs["min_stars"]["default"] == "100"
    assert inputs["active_within_days"]["default"] == "365"
    assert inputs["max_pages"]["default"] == "10"

    discover = workflow["jobs"]["discover"]
    assert discover["env"] == {
        "ACTIVE_WITHIN_DAYS": "${{ inputs.active_within_days || 365 }}",
        "MAX_PAGES": "${{ inputs.max_pages || 10 }}",
        "MIN_STARS": "${{ inputs.min_stars || 100 }}",
        "WORKSPACE": "catalog-workspace",
    }
    commands = "\n".join(step.get("run", "") for step in discover["steps"])
    assert "ghmod refresh" in commands
    assert '--min-stars "$MIN_STARS"' in commands
    assert '--active-within-days "$ACTIVE_WITHIN_DAYS"' in commands
    assert '--max-pages "$MAX_PAGES"' in commands
    assert "ghmod validate-output" in commands
    assert "ghmod discover" not in commands
    assert "ghmod classify" not in commands


def test_discovery_and_publication_jobs_have_isolated_least_privilege() -> None:
    workflow = _discovery_workflow()
    assert workflow["permissions"] == {}
    discover = workflow["jobs"]["discover"]
    publish = workflow["jobs"]["publish"]
    assert discover["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"contents": "write"}
    assert publish["needs"] == "discover"
    assert publish["if"] == (
        "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    )

    text = (ROOT / ".github" / "workflows" / "discover.yml").read_text(encoding="utf-8")
    assert "actions/cache/" not in text
    assert "catalog-workspace/data" not in text
    assert "retention-days: 1" in text
    assert "path: catalog-workspace/catalog-output" in text
    assert "persist-credentials: false" in text
    assert "ref: ${{ github.sha }}" in text
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in text
    assert "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990" in text
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in text


def test_publish_job_promotes_tracked_catalog_with_restricted_non_force_push() -> None:
    workflow = _discovery_workflow()
    discover = workflow["jobs"]["discover"]
    publish = workflow["jobs"]["publish"]
    commands = "\n".join(step.get("run", "") for step in publish["steps"])
    uses = [step.get("uses") for step in publish["steps"] if "uses" in step]
    upload = next(
        step for step in discover["steps"] if "actions/upload-artifact@" in step.get("uses", "")
    )
    download = next(
        step for step in publish["steps"] if "actions/download-artifact@" in step.get("uses", "")
    )

    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in uses
    assert upload["with"]["name"] == download["with"]["name"]
    assert upload["with"]["path"] == "catalog-workspace/catalog-output"
    assert "python3 scripts/publish_catalog.py" in commands
    assert "git add -- README.md catalog/" in commands
    assert "git add -A" not in commands
    assert 'git push origin "HEAD:${{ github.event.repository.default_branch }}"' in commands
    assert "--force" not in commands


def test_homepage_defines_ranked_discovery_and_a_tracked_catalog_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert readme.count("<!-- catalog-index:begin -->") == 1
    assert readme.count("<!-- catalog-index:end -->") == 1
    assert "stars:>=100" in readme
    assert "365" in readme
    assert "1,000" in readme
    assert "not an exhaustive" in readme
    assert "catalog/catalog.json" in readme
    assert "Generated datasets do not live in Git" not in readme


def test_ci_scans_current_tree_for_strong_secret_shapes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Scan current tree for strong secret patterns" in workflow
    assert "git grep" in workflow
    assert "git grep -qE" in workflow
    assert "AKIA[A-Z0-9]" in workflow
    assert "BEGIN [A-Z ]*PRIVATE KEY" in workflow
    assert "gh[pousr]_[A-Za-z0-9]{20,}" in workflow
    assert "github_pat_[A-Za-z0-9_]{20,}" in workflow
    assert "scan_status=$?" in workflow
    assert 'case "$scan_status" in' in workflow
    assert "Scanner execution failed" in workflow
