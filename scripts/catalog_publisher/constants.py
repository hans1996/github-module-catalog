"""Shared limits and catalog syntax for repository publication."""

from __future__ import annotations

import re

BEGIN_MARKER = b"<!-- catalog-index:begin -->"
END_MARKER = b"<!-- catalog-index:end -->"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
MODULE_PATH_PATTERN = re.compile(r"^modules/[a-z0-9][a-z0-9-]{0,99}\.md$")

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_README_BYTES = 4 * 1024 * 1024
MAX_ARTIFACTS = 10_000
SEARCH_RESULTS_PER_PAGE = 100
