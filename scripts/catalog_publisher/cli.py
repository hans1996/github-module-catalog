"""Command-line interface for tracked catalog publication."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from .model import PublicationError
from .publication import publish_to_repository


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and atomically promote a catalog into a repository homepage."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    return parser.parse_args(argv)


def _fatal() -> NoReturn:
    print("Error: catalog publication failed safely", file=sys.stderr)
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        summary = publish_to_repository(args.source, args.repository)
    except (PublicationError, OSError, ValueError):
        _fatal()
    print(
        json.dumps(
            {
                "capabilities": summary.capabilities,
                "catalog_files": summary.catalog_files,
                "entries": summary.entries,
                "status": "published",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
