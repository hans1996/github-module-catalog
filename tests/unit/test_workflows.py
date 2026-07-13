"""Read-only assertions for pinned least-privilege workflows."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
NODE24_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/cache/restore": "27d5ce7f107fe9357f9df03efb73ab90386fccae",
    "actions/cache/save": "27d5ce7f107fe9357f9df03efb73ab90386fccae",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv": "cec208311dfd045dd5311c1add060b2062131d57",
}


def test_workflows_use_reviewed_full_node24_action_pins() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )
    observed = re.findall(r"uses:\s+([^@\s]+)@([0-9a-f]{40})", workflows)

    assert observed
    for action, commit in observed:
        assert NODE24_ACTIONS[action] == commit


def test_discovery_uploads_complete_provenance_only_after_success() -> None:
    workflow = (ROOT / ".github" / "workflows" / "discover.yml").read_text(
        encoding="utf-8"
    )

    assert "if: always()" not in workflow
    assert "if: success()" in workflow
    assert "catalog-workspace/data" in workflow
    assert "catalog-workspace/data/state.sqlite3*" not in workflow
    assert "retention-days: 3" in workflow


def test_ci_scans_current_tree_for_strong_secret_shapes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Scan current tree for strong secret patterns" in workflow
    assert "git grep" in workflow
    assert "AKIA[A-Z0-9]" in workflow
    assert "BEGIN [A-Z ]*PRIVATE KEY" in workflow
