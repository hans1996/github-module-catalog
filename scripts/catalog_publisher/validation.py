"""Artifact integrity, ranked schema, and homepage validation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath

from .constants import (
    BEGIN_MARKER,
    CAPABILITY_PATTERN,
    END_MARKER,
    MAX_ARTIFACTS,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_BYTES,
    MODULE_PATH_PATTERN,
    SEARCH_RESULTS_PER_PAGE,
    SHA256_PATTERN,
    UTC_TIMESTAMP_PATTERN,
)
from .filesystem import (
    _open_directory,
    _read_regular_at,
    _read_relative_regular,
    _tree_entries,
)
from .model import PublicationError, ValidatedPublication


def _validate_source(source: Path) -> ValidatedPublication:
    source_fd = _open_directory(source, "catalog source")
    try:
        manifest_bytes, _ = _read_regular_at(
            source_fd,
            "manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        manifest = _json_object(manifest_bytes, "artifact manifest")
        artifacts = _artifact_mapping(manifest)
        expected_files = set(artifacts) | {"manifest.json"}
        actual_files, actual_directories = _tree_entries(source_fd)
        expected_directories = {
            PurePosixPath(name).parent.as_posix()
            for name in expected_files
            if PurePosixPath(name).parent.as_posix() != "."
        }
        if actual_files != expected_files or actual_directories != expected_directories:
            raise PublicationError("catalog artifact file set differs from its manifest")

        contents: dict[str, bytes] = {"manifest.json": manifest_bytes}
        total_bytes = len(manifest_bytes)
        for name, expected_digest in artifacts.items():
            content = _read_relative_regular(source_fd, name)
            total_bytes += len(content)
            if total_bytes > MAX_TOTAL_BYTES:
                raise PublicationError("catalog publication exceeds the total byte limit")
            if hashlib.sha256(content).hexdigest() != expected_digest:
                raise PublicationError("catalog artifact digest differs from its manifest")
            contents[name] = content

        catalog = _json_object(contents["catalog.json"], "catalog JSON")
        manifest_fields = {key: value for key, value in manifest.items() if key != "artifacts"}
        catalog_fields = {key: value for key, value in catalog.items() if key != "entries"}
        if _canonical_json(manifest_fields) != _canonical_json(catalog_fields):
            raise PublicationError("catalog manifest metadata differs from catalog JSON")
        entries, capability_counts = _validate_catalog_metadata(catalog, set(artifacts))
        homepage = _render_homepage(catalog, capability_counts)
        return ValidatedPublication(
            contents=dict(sorted(contents.items())),
            homepage=homepage,
            entries=entries,
            capabilities=len(capability_counts),
        )
    finally:
        os.close(source_fd)


def _artifact_mapping(manifest: dict[str, object]) -> dict[str, str]:
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, dict) or not 1 <= len(raw_artifacts) <= MAX_ARTIFACTS:
        raise PublicationError("artifact manifest has no bounded artifact mapping")
    artifacts: dict[str, str] = {}
    for raw_name, raw_digest in raw_artifacts.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise PublicationError("artifact manifest contains an invalid entry")
        name = _safe_artifact_name(raw_name)
        if name == "manifest.json" or SHA256_PATTERN.fullmatch(raw_digest) is None:
            raise PublicationError("artifact manifest contains an unsafe entry")
        artifacts[name] = raw_digest
    required = {"README.md", "catalog.json", "catalog.yaml"}
    if not required.issubset(artifacts):
        raise PublicationError("ranked publication requires Markdown, JSON, and YAML catalogs")
    return artifacts


def _safe_artifact_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (
            value not in {"README.md", "catalog.json", "catalog.yaml"}
            and MODULE_PATH_PATTERN.fullmatch(value) is None
        )
    ):
        raise PublicationError("artifact manifest contains an unsafe path")
    return value


def _validate_catalog_metadata(
    catalog: dict[str, object], artifact_names: set[str]
) -> tuple[int, dict[str, int]]:
    if catalog.get("source") != "github-search-repositories":
        raise PublicationError("catalog source is not the ranked GitHub Search source")
    for key in ("schema_version", "taxonomy_version", "classifier_version"):
        if not isinstance(catalog.get(key), str) or not catalog[key]:
            raise PublicationError("catalog version metadata is invalid")
    _parse_utc_timestamp(catalog.get("generated_at"), "generated_at")
    selection = catalog.get("selection")
    if not isinstance(selection, dict):
        raise PublicationError("catalog selection policy is missing")
    min_stars = _strict_int(selection.get("min_stars"), minimum=0, name="min_stars")
    pushed_since = _parse_utc_timestamp(selection.get("pushed_since"), "pushed_since")
    if (
        selection.get("exclude_archived") is not True
        or selection.get("exclude_forks") is not True
        or selection.get("public_only") is not True
        or selection.get("sort") != "stars"
        or selection.get("order") != "desc"
    ):
        raise PublicationError("catalog selection policy is weaker than ranked publication")
    result_limit = _strict_int(selection.get("result_limit"), minimum=1, name="result_limit")
    if result_limit > 1_000 or catalog.get("result_limit") != result_limit:
        raise PublicationError("catalog result limit is invalid")
    api_total_count = _strict_int(catalog.get("api_total_count"), minimum=0, name="api_total_count")
    pages_fetched = _strict_int(catalog.get("pages_fetched"), minimum=1, name="pages_fetched")
    if pages_fetched > 10:
        raise PublicationError("catalog Search page count exceeds GitHub's ranked window")

    entries_value = catalog.get("entries")
    if not isinstance(entries_value, list):
        raise PublicationError("catalog entries are invalid")
    entry_count = _strict_int(catalog.get("entry_count"), minimum=0, name="entry_count")
    target_count = min(api_total_count, result_limit)
    if entry_count != len(entries_value) or entry_count != target_count:
        raise PublicationError("catalog entry count differs from ranked entries")
    for count_name in ("discovered_count", "validated_observation_count"):
        if _strict_int(catalog.get(count_name), minimum=0, name=count_name) != entry_count:
            raise PublicationError("catalog coverage counts differ from ranked entries")
    expected_pages = max(
        1,
        (target_count + SEARCH_RESULTS_PER_PAGE - 1) // SEARCH_RESULTS_PER_PAGE,
    )
    if pages_fetched != expected_pages:
        raise PublicationError("catalog Search page coverage is inconsistent")
    _validate_search_page_evidence(
        catalog,
        pages_fetched=pages_fetched,
        min_stars=min_stars,
        pushed_since=pushed_since,
    )
    repository_ids = _validate_ranked_entries(
        entries_value,
        min_stars=min_stars,
        pushed_since=pushed_since,
    )
    _validate_ranked_provenance(catalog, repository_ids)

    capability_counts: dict[str, int] = {}
    for entry in entries_value:
        if not isinstance(entry, dict) or not isinstance(entry.get("assertions"), list):
            raise PublicationError("catalog entry assertions are invalid")
        seen: set[str] = set()
        for assertion in entry["assertions"]:
            if not isinstance(assertion, dict):
                raise PublicationError("catalog assertion is invalid")
            capability = assertion.get("capability_id")
            if not isinstance(capability, str) or CAPABILITY_PATTERN.fullmatch(capability) is None:
                raise PublicationError("catalog capability ID is unsafe")
            if capability in seen:
                raise PublicationError("catalog entry repeats a capability")
            seen.add(capability)
            capability_counts[capability] = capability_counts.get(capability, 0) + 1
    if catalog.get("capability_count") != len(capability_counts):
        raise PublicationError("catalog capability count differs from assertions")
    expected_modules = {f"modules/{capability}.md" for capability in capability_counts}
    observed_modules = {name for name in artifact_names if name.startswith("modules/")}
    if expected_modules != observed_modules:
        raise PublicationError("catalog module pages differ from capability assertions")
    return entry_count, dict(sorted(capability_counts.items()))


def _validate_search_page_evidence(
    catalog: dict[str, object],
    *,
    pages_fetched: int,
    min_stars: int,
    pushed_since: datetime,
) -> None:
    search_pages = catalog.get("search_pages")
    if not isinstance(search_pages, list) or len(search_pages) != pages_fetched:
        raise PublicationError("catalog Search page evidence is incomplete")
    expected_query = (
        f"stars:>={min_stars} pushed:>={pushed_since.date().isoformat()} archived:false is:public"
    )
    evidence_hashes: list[str] = []
    for expected_number, page in enumerate(search_pages, start=1):
        if (
            not isinstance(page, dict)
            or page.get("page_number") != expected_number
            or page.get("query") != expected_query
        ):
            raise PublicationError("catalog Search page evidence differs from its policy")
        raw_sha256 = page.get("raw_sha256")
        if not isinstance(raw_sha256, str) or SHA256_PATTERN.fullmatch(raw_sha256) is None:
            raise PublicationError("catalog Search page evidence has an invalid digest")
        evidence_hashes.append(raw_sha256)
    if len(evidence_hashes) != len(set(evidence_hashes)):
        raise PublicationError("catalog Search page evidence repeats a raw response")

    raw_page_hashes = catalog.get("raw_page_hashes")
    if (
        not isinstance(raw_page_hashes, list)
        or raw_page_hashes != sorted(set(raw_page_hashes))
        or set(raw_page_hashes) != set(evidence_hashes)
    ):
        raise PublicationError("catalog raw page hashes differ from Search evidence")


def _validate_ranked_entries(
    entries: list[object],
    *,
    min_stars: int,
    pushed_since: datetime,
) -> tuple[int, ...]:
    repository_ids: list[int] = []
    rank_keys: list[tuple[int, int]] = []
    for expected_rank, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise PublicationError("catalog ranked entry is invalid")
        if _strict_int(entry.get("rank"), minimum=1, name="rank") != expected_rank:
            raise PublicationError("catalog ranks are not contiguous from one")
        repository = entry.get("repository")
        if not isinstance(repository, dict):
            raise PublicationError("catalog ranked repository facts are missing")
        identity = repository.get("identity")
        if not isinstance(identity, dict):
            raise PublicationError("catalog repository identity is missing")
        repository_id = _strict_int(identity.get("repository_id"), minimum=1, name="repository_id")
        stars = _strict_int(repository.get("stargazers_count"), minimum=0, name="stargazers_count")
        pushed_at = _parse_utc_timestamp(repository.get("pushed_at"), "pushed_at")
        if (
            stars < min_stars
            or pushed_at < pushed_since
            or repository.get("archived") is not False
            or repository.get("fork") is not False
            or repository.get("private") is not False
        ):
            raise PublicationError("catalog repository does not satisfy ranked selection")
        repository_ids.append(repository_id)
        rank_keys.append((-stars, repository_id))
    if len(repository_ids) != len(set(repository_ids)):
        raise PublicationError("catalog repeats a repository identity")
    if rank_keys != sorted(rank_keys):
        raise PublicationError("catalog entries do not follow deterministic rank order")
    return tuple(repository_ids)


def _validate_ranked_provenance(
    catalog: dict[str, object], repository_ids: tuple[int, ...]
) -> None:
    source_hashes = catalog.get("source_hashes")
    if (
        not isinstance(source_hashes, list)
        or len(source_hashes) != len(repository_ids)
        or source_hashes != sorted(set(source_hashes))
        or any(
            not isinstance(item, str) or SHA256_PATTERN.fullmatch(item) is None
            for item in source_hashes
        )
    ):
        raise PublicationError("catalog source hashes differ from ranked repositories")
    failures = catalog.get("classification_failure_repository_ids")
    if not isinstance(failures, list) or any(type(item) is not int for item in failures):
        raise PublicationError("catalog classification failure identities are invalid")
    if failures != sorted(set(failures)) or not set(failures).issubset(repository_ids):
        raise PublicationError("catalog classification failures differ from ranked repositories")


def _render_homepage(catalog: dict[str, object], capability_counts: dict[str, int]) -> bytes:
    selection = catalog["selection"]
    if not isinstance(selection, dict):  # pragma: no cover - validated by the caller
        raise PublicationError("catalog selection policy is missing")
    min_stars = _strict_int(selection["min_stars"], minimum=0, name="min_stars")
    entry_count = _strict_int(catalog["entry_count"], minimum=0, name="entry_count")
    api_total = _strict_int(catalog["api_total_count"], minimum=0, name="api_total_count")
    pushed_since = (
        _parse_utc_timestamp(selection["pushed_since"], "pushed_since").date().isoformat()
    )
    generated_at = _parse_utc_timestamp(catalog["generated_at"], "generated_at").strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    lines = [
        "## Live catalog",
        "",
        "| Indexed repositories | GitHub Search matches | Last refresh |",
        "| ---: | ---: | --- |",
        f"| **{entry_count:,}** | **{api_total:,}** | **{generated_at}** |",
        "",
        f"**Selection:** **{min_stars:,}+ stars** · pushed since **{pushed_since}** · public · "
        "non-archived · non-fork",
        "",
        "**Ranking:** stars descending, then repository ID. This is a top-ranked window, "
        "not an exhaustive index of GitHub.",
        "",
        "[Browse the full catalog](catalog/README.md) · "
        "[JSON](catalog/catalog.json) · [YAML](catalog/catalog.yaml)",
        "",
        "### Browse by capability",
        "",
        "Capability groups overlap; one repository may appear in more than one group.",
        "",
    ]
    if capability_counts:
        lines.extend(
            f"- [`{capability}`](catalog/modules/{capability}.md) — {count:,}"
            for capability, count in capability_counts.items()
        )
    else:
        lines.append("No capability assertions were produced for this snapshot.")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _replace_managed_section(readme: bytes, homepage: bytes) -> bytes:
    if readme.count(BEGIN_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise PublicationError("README must contain exactly one catalog marker pair")
    lines = readme.splitlines(keepends=True)
    begin_lines = [
        index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == BEGIN_MARKER
    ]
    end_lines = [index for index, line in enumerate(lines) if line.rstrip(b"\r\n") == END_MARKER]
    if len(begin_lines) != 1 or len(end_lines) != 1 or begin_lines[0] >= end_lines[0]:
        raise PublicationError("README catalog markers are malformed or reversed")
    begin_index = begin_lines[0]
    end_index = end_lines[0]
    if not lines[begin_index].endswith((b"\n", b"\r")):
        raise PublicationError("README begin marker must occupy its own line")
    prefix = b"".join(lines[: begin_index + 1])
    suffix = b"".join(lines[end_index:])
    return prefix + b"\n" + homepage + suffix


def _json_object(content: bytes, label: str) -> dict[str, object]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublicationError(f"{label} is not valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise PublicationError(f"{label} must contain an object")
    return document


def _canonical_json(document: object) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _strict_int(value: object, *, minimum: int, name: str) -> int:
    if type(value) is not int or value < minimum:
        raise PublicationError(f"catalog {name} is invalid")
    return value


def _utc_timestamp(value: object, name: str) -> str:
    parsed = _parse_utc_timestamp(value, name)
    return parsed.isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise PublicationError(f"catalog {name} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PublicationError(f"catalog {name} is not a UTC timestamp") from None
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise PublicationError(f"catalog {name} is not a UTC timestamp")
    return parsed
