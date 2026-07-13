"""Shared publication value objects and identity types."""

from __future__ import annotations

from dataclasses import dataclass

type NodeIdentity = tuple[int, int, int]
type FileIdentity = tuple[int, int, int, int, int, int]


class PublicationError(RuntimeError):
    """A safe failure raised before unvalidated data can reach the repository."""


@dataclass(frozen=True, slots=True)
class PublicationSummary:
    """Non-secret facts about one successful repository promotion."""

    entries: int
    capabilities: int
    catalog_files: int


@dataclass(frozen=True, slots=True)
class ValidatedPublication:
    """Validated immutable inputs ready for repository promotion."""

    contents: dict[str, bytes]
    homepage: bytes
    entries: int
    capabilities: int
