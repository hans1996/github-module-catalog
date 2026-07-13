# Operations guide

GitHub Module Catalog advances through bounded, resumable discovery runs. It
does not claim to have indexed all of GitHub until measured cursor coverage
supports that claim.

## Credentials

Use a short-lived or revocable GitHub credential with only the access needed to
read public repository metadata. For a local session, obtain the credential
from the authenticated GitHub CLI without displaying it:

```bash
export GITHUB_TOKEN="$(gh auth token)"
```

Do not use `echo`, shell tracing, command-line token flags, committed `.env`
files, or URLs containing credentials. `GITHUB_TOKEN` is read only by the
`discover` command and is passed directly to the GitHub adapter. The supplied
token is never written to SQLite, raw objects, catalog output, logs, or command
summaries.

Raw snapshots and caches preserve exact untrusted public metadata. A public
description can itself contain accidentally published credential-shaped text,
so raw data is quarantined source evidence: never render it, grant the narrowest
artifact access, and use the shortest retention compatible with recovery.

## Local runbook

```bash
uv sync --all-groups --locked
uv run ghmod init --workspace .local/catalog
uv run ghmod discover --workspace .local/catalog --max-pages 10
uv run ghmod status --workspace .local/catalog
uv run ghmod classify --workspace .local/catalog
uv run ghmod build --workspace .local/catalog
uv run ghmod validate --workspace .local/catalog
```

`build` publishes JSON, YAML, and Markdown by default. Repeat `--format` to
publish an exact subset, for example `--format json --format markdown`.
`manifest.json` is always emitted and lists hashes for exactly the selected
artifacts. Every build requires at least one machine-readable catalog (`json`
or `yaml`), and Markdown may accompany either. When JSON and YAML are both
selected, validation requires them to be equivalent.

`--max-pages` is required, must be between 1 and 1,000, and is the hard request
budget for one invocation. Start with a small value. GitHub rate-limit headers
are captured as source facts; `403` and `429` responses produce a typed retry
decision rather than sleeping inside the adapter.

## Cursor and recovery semantics

The CLI binds discovery, status, build, and validation to the trusted source ID
`github`. Catalog output naming any other source is rejected before a
source-scoped state snapshot can be accepted.

Discovery uses `GET /repositories?since=<repository-id>` and follows only the
allowlisted `rel="next"` URL returned by GitHub. The numeric cursor moves only
after all of these steps succeed:

1. the exact response bytes pass boundary validation;
2. the bytes are durably stored by SHA-256;
3. page metadata and every repository identity commit in one SQLite
   transaction.

Metadata-to-observation processing happens after that durable discovery commit.
A per-repository observation failure records a safe `retry` event and does not
roll back or stall the cursor. Re-running discovery resumes from the latest
committed source-scoped cursor. If a process stops before the page transaction
commits, the same page is fetched again and numeric identities remain
idempotent.

The scheduled workflow restores and saves `catalog-workspace/data` with a
run-specific cache key and a stable restore prefix. It saves a new checkpoint
only after discovery, build, and validation succeed. The output and SQLite
state plus provenance-required raw objects are uploaded only after validation
as short-retention workflow artifacts. Treat workspace caches and artifacts as
sensitive untrusted metadata even though catalog output passed secret-shape
rejection. Generated datasets are never committed to Git.

For local corruption recovery, preserve the failed workspace for diagnosis,
then restore the last validated `data` artifact or initialize a new workspace.
Never hand-edit the cursor. `ghmod validate` returns non-zero when required
files, JSON/YAML equivalence, schema checks, artifact paths, or SHA-256 hashes
do not match.

## Coverage and deferred enrichment

The public repository feed is broad but each run is intentionally bounded.
`status`, `catalog.json`, and `manifest.json` report the cursor, discovered
identity count, validated observation count, and pending/retry/dead-letter
work. These figures are coverage evidence, not proof that the catalog contains
every public repository or every reusable module.

Inventory records with a complete metadata set become initial validated
observations immediately. Identity-only records remain discoverable and queued
for deferred enrichment; the crawler does not invent missing description,
language, topic, lifecycle, timestamp, or license facts. Future enrichment may
use repository-detail endpoints under separate rate budgets and checkpoints.

## License and execution safety

Public visibility is not reuse permission. A missing, unknown, archived, or
disabled license/lifecycle signal remains `discovery_only`. Only an explicitly
recognized permissive SPDX signal can become `safe_to_integrate`; this remains
a technical policy signal, not legal advice.

Repository content is untrusted data. Discovery never clones a repository or
executes its code, workflows, package scripts, build files, or embedded
instructions. Workspace and output symlinks are rejected, GitHub API hosts are
allowlisted, response bodies are bounded, and rendered Markdown excludes
untrusted descriptions.

## GitHub App migration

The initial workflow uses the repository-scoped `secrets.GITHUB_TOKEN`. At
larger scale, replace the CLI source factory's token input with a GitHub App
installation-token provider. Keep token creation at the command boundary,
request read-only metadata permissions, rotate short-lived installation tokens,
and preserve the existing redaction, host allowlist, page budget, cursor, and
durability contracts. Do not put App private keys in the catalog workspace.
