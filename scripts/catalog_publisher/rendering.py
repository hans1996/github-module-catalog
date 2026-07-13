"""Pure-stdlib canonical Markdown rendering for the write-capable publisher."""

from __future__ import annotations

import html
import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .constants import CAPABILITY_PATTERN, MAX_TAXONOMY_RENDER_ROWS
from .model import PublicationError

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,38}$")
_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REUSE_STATUSES = frozenset({"discovery_only", "safe_to_integrate"})


def canonical_markdown_artifacts(catalog: dict[str, object]) -> dict[str, bytes]:
    """Reconstruct every publishable Markdown artifact from catalog JSON."""

    entries = _object_list(catalog.get("entries"), "entries")
    capabilities = sorted(
        {
            _capability_id(assertion.get("capability_id"))
            for entry in entries
            for assertion in _object_list(entry.get("assertions"), "assertions")
        }
    )
    artifacts = {
        "README.md": _render_readme(catalog, entries).encode("utf-8"),
        "taxonomy.md": _render_taxonomy_page(catalog, entries).encode("utf-8"),
    }
    artifacts.update(
        {
            f"modules/{capability_id}.md": _render_module_page(
                catalog,
                entries,
                capability_id,
            ).encode("utf-8")
            for capability_id in capabilities
        }
    )
    return dict(sorted(artifacts.items()))


def _render_readme(catalog: dict[str, object], entries: list[dict[str, object]]) -> str:
    lines = [
        "# GitHub Module Catalog",
        "",
        _untrusted_markdown_inline(_string(catalog.get("coverage_note"), "coverage_note")),
        "",
        "[Capability taxonomy](taxonomy.md)",
        "",
        *_selection_summary(catalog),
        f"Source: {_markdown_code_span(_string(catalog.get('source'), 'source'))}.",
        "",
        "| Rank | Stars | Last push | Repository | Capabilities | License | Reuse status |",
        "| ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    lines.extend(_ranked_entry_row(entry) for entry in entries)
    return _newline("\n".join(lines))


def _render_taxonomy_page(catalog: dict[str, object], entries: list[dict[str, object]]) -> str:
    raw_definitions = _object_list(
        catalog.get("capability_definitions"),
        "capability_definitions",
    )
    definitions = {
        _capability_id(definition.get("id")): definition for definition in raw_definitions
    }
    children = {
        capability_id: tuple(
            sorted(
                _capability_id(definition.get("id"))
                for definition in raw_definitions
                if capability_id in _string_list(definition.get("parents"), "parents")
            )
        )
        for capability_id in definitions
    }
    counts = _capability_counts(entries)
    roots = tuple(
        _capability_id(definition.get("id"))
        for definition in raw_definitions
        if not _string_list(definition.get("parents"), "parents")
    )
    taxonomy_version = _markdown_code_span(
        _string(catalog.get("taxonomy_version"), "taxonomy_version")
    )
    classifier_version = _markdown_code_span(
        _string(catalog.get("classifier_version"), "classifier_version")
    )
    lines = [
        "# Capability taxonomy",
        "",
        f"Taxonomy version: {taxonomy_version}. Classifier: {classifier_version}.",
        "",
        "Repositories can appear in multiple capability branches. Parent counts include "
        "repositories assigned to descendant capabilities.",
        "",
        "## Capability map",
        "",
    ]
    rendered_rows = [0]
    for capability_id in roots:
        lines.extend(
            _taxonomy_branch_lines(
                capability_id,
                depth=0,
                definitions=definitions,
                children=children,
                counts=counts,
                rendered_rows=rendered_rows,
            )
        )
    if not roots:
        lines.append("No capability definitions were published for this snapshot.")
    return _newline("\n".join(lines))


def _render_module_page(
    catalog: dict[str, object],
    entries: list[dict[str, object]],
    capability_id: str,
) -> str:
    matching = tuple(
        (entry, assertion)
        for entry in entries
        for assertion in _object_list(entry.get("assertions"), "assertions")
        if assertion.get("capability_id") == capability_id
    )
    lines = [
        f"# {_markdown_code_span(capability_id)} modules",
        "",
        *_selection_summary(catalog),
        "| Rank | Stars | Last push | Repository | Confidence | License | Reuse status |",
        "| ---: | ---: | --- | --- | ---: | --- | --- |",
    ]
    for entry, assertion in matching:
        confidence = _confidence(assertion.get("confidence"))
        reuse_status = _reuse_status(assertion.get("reuse_status"))
        lines.append(
            "| "
            f"{_rank(entry)} | {_stars(entry)} | {_last_push(entry)} | "
            f"{_repository_link(entry)} | {confidence:.2f} | {_license(entry)} | "
            f"`{reuse_status}` |"
        )
    return _newline("\n".join(lines))


def _ranked_entry_row(entry: dict[str, object]) -> str:
    capabilities = ", ".join(
        f"`{_markdown_text(_capability_id(assertion.get('capability_id')))}`"
        for assertion in _object_list(entry.get("assertions"), "assertions")
    )
    repository = _mapping(entry.get("repository"), "repository")
    reuse_status = _reuse_status(repository.get("reuse_status"))
    return (
        "| "
        f"{_rank(entry)} | {_stars(entry)} | {_last_push(entry)} | "
        f"{_repository_link(entry)} | "
        f"{capabilities or '—'} | {_license(entry)} | "
        f"`{reuse_status}` |"
    )


def _selection_summary(catalog: dict[str, object]) -> list[str]:
    selection = _mapping(catalog.get("selection"), "selection")
    return [
        "## Selection",
        "",
        f"Minimum stars: `{_integer(selection.get('min_stars'), 'min_stars')}`; "
        f"Pushed since: `{_utc_timestamp(selection.get('pushed_since'), 'pushed_since')}`.",
        "Archived: `false`; forks: `false`; visibility: `public`; "
        f"Order: `{_string(selection.get('sort'), 'sort')} "
        f"{_string(selection.get('order'), 'order')}`.",
        f"Top `{_integer(catalog.get('entry_count'), 'entry_count')}` of "
        f"`{_integer(catalog.get('api_total_count'), 'api_total_count')}` matching repositories; "
        f"result limit: `{_integer(catalog.get('result_limit'), 'result_limit')}`; "
        f"pages fetched: `{_integer(catalog.get('pages_fetched'), 'pages_fetched')}`.",
        "",
    ]


def _taxonomy_branch_lines(
    capability_id: str,
    *,
    depth: int,
    definitions: dict[str, dict[str, object]],
    children: dict[str, tuple[str, ...]],
    counts: dict[str, int],
    rendered_rows: list[int],
) -> list[str]:
    rendered_rows[0] += 1
    if rendered_rows[0] > MAX_TAXONOMY_RENDER_ROWS:
        raise PublicationError("catalog capability hierarchy expands beyond the render limit")
    definition = definitions[capability_id]
    label = _markdown_text(_string(definition.get("label"), "capability label"))
    count = counts.get(capability_id, 0)
    reference = _markdown_code_span(capability_id)
    if count:
        reference = f"[{reference}](modules/{capability_id}.md)"
    lines = [f"{'  ' * depth}- {reference} — {label} — {count:,}"]
    for child_id in children[capability_id]:
        lines.extend(
            _taxonomy_branch_lines(
                child_id,
                depth=depth + 1,
                definitions=definitions,
                children=children,
                counts=counts,
                rendered_rows=rendered_rows,
            )
        )
    return lines


def _capability_counts(entries: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        for assertion in _object_list(entry.get("assertions"), "assertions"):
            capability_id = _capability_id(assertion.get("capability_id"))
            counts[capability_id] = counts.get(capability_id, 0) + 1
    return dict(sorted(counts.items()))


def _rank(entry: dict[str, object]) -> str:
    return str(_integer(entry.get("rank"), "rank"))


def _stars(entry: dict[str, object]) -> str:
    repository = _mapping(entry.get("repository"), "repository")
    return str(_integer(repository.get("stargazers_count"), "stargazers_count"))


def _last_push(entry: dict[str, object]) -> str:
    repository = _mapping(entry.get("repository"), "repository")
    return _utc_timestamp(repository.get("pushed_at"), "pushed_at")


def _repository_link(entry: dict[str, object]) -> str:
    repository = _mapping(entry.get("repository"), "repository")
    owner = _string(repository.get("owner"), "owner")
    name = _string(repository.get("name"), "name")
    full_name = _string(repository.get("full_name"), "full_name")
    html_url = _string(repository.get("html_url"), "html_url")
    if (
        _OWNER_PATTERN.fullmatch(owner) is None
        or _REPOSITORY_NAME_PATTERN.fullmatch(name) is None
        or full_name != f"{owner}/{name}"
        or not _safe_github_url(html_url, owner=owner, name=name)
    ):
        raise PublicationError("catalog Markdown repository link is invalid")
    return f"[{_markdown_text(full_name)}]({html_url})"


def _safe_github_url(value: str, *, owner: str, name: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"github.com", "www.github.com"}
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
        and parsed.path.rstrip("/") == f"/{owner}/{name}"
    )


def _license(entry: dict[str, object]) -> str:
    repository = _mapping(entry.get("repository"), "repository")
    value = repository.get("license_spdx")
    if value is None:
        value = "unknown"
    return _markdown_code_span(_string(value, "license_spdx"))


def _capability_id(value: object) -> str:
    capability_id = _string(value, "capability_id")
    if CAPABILITY_PATTERN.fullmatch(capability_id) is None:
        raise PublicationError("catalog Markdown capability ID is invalid")
    return capability_id


def _reuse_status(value: object) -> str:
    reuse_status = _string(value, "reuse_status")
    if reuse_status not in _REUSE_STATUSES:
        raise PublicationError("catalog Markdown reuse status is invalid")
    return reuse_status


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicationError("catalog Markdown confidence is invalid")
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise PublicationError("catalog Markdown confidence is invalid")
    return confidence


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return value


def _object_list(value: object, name: str) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return [_mapping(item, name) for item in value]


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return value


def _integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return value


def _utc_timestamp(value: object, name: str) -> str:
    raw = _string(value, name)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00") if raw.endswith("Z") else None
    except ValueError:
        parsed = None
    offset = None if parsed is None else parsed.utcoffset()
    if parsed is None or offset is None or offset.total_seconds() != 0:
        raise PublicationError(f"catalog Markdown {name} is invalid")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _markdown_text(value: str) -> str:
    value = html.escape(value, quote=True)
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _markdown_code_span(value: str) -> str:
    collapsed = html.escape(re.sub(r"[\r\n]+", " ", value), quote=True)
    longest_run = max((len(run) for run in re.findall(r"`+", collapsed)), default=0)
    delimiter = "`" * (longest_run + 1)
    if longest_run == 0 and not collapsed.startswith(" ") and not collapsed.endswith(" "):
        return f"{delimiter}{collapsed}{delimiter}"
    return f"{delimiter} {collapsed} {delimiter}"


def _untrusted_markdown_inline(value: str) -> str:
    collapsed = re.sub(r"[\r\n]+", " ", value)
    html_safe = html.escape(collapsed, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", html_safe)


def _newline(value: str) -> str:
    return value.rstrip("\n") + "\n"
