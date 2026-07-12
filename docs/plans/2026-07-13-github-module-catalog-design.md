# GitHub Module Catalog Design

**Date:** 2026-07-13  
**Status:** Approved for MVP implementation

## Purpose and honest scope

GitHub Module Catalog discovers public GitHub repositories and turns their
observable metadata into a versioned catalog of reusable capabilities. The
system does not equate a repository with one module: a monorepo may expose many
libraries, services, plugins, templates, or command-line tools, while one
capability may have many competing implementations.

"All GitHub" is a long-running coverage objective, not a one-shot API query.
The system must always report what repository-ID interval it has discovered,
what items remain queued for enrichment, what failed, and which catalog build
was produced from which source observations. Search results, stars, and topics
are useful for prioritization but cannot prove discovery completeness.

The MVP therefore separates broad, inexpensive discovery from bounded, more
expensive enrichment. It will enumerate public repositories, retain enough
metadata to decide priority, deeply analyze only an explicitly bounded cohort,
and publish measurable progress. Future sources such as public event datasets
or archival snapshots can implement the same source protocol without changing
the catalog domain model.

## Architecture and components

The system is a Python 3.12 CLI with strict layers:

1. **Discovery source:** `GET /repositories?since=<id>` is the canonical public
   repository feed. A monotonically increasing numeric ID is the durable
   cursor. Search API queries are optional priority inputs only.
2. **Raw storage:** every fetched page is stored as immutable, content-addressed
   JSON with request metadata, ETag, fetch time, API version, and SHA-256.
3. **State store:** SQLite tracks crawl runs, page commits, stage checkpoints,
   work-item events, retry state, and publication manifests. The cursor advances
   only after the raw page and queued repository identities are committed.
4. **Enrichment:** selected repositories receive additional metadata such as
   topics, license, lifecycle flags, default branch, and later manifest or
   README evidence. No downloaded code is executed.
5. **Classifier:** deterministic rules produce multi-axis capability
   assertions. Assertions record confidence, evidence, taxonomy version,
   classifier version, and the source observation used.
6. **Exporter:** deterministic JSON, YAML, and Markdown views are generated from
   completed observations. Large datasets are release artifacts, not Git
   history.

All public domain models are immutable. External responses are validated at the
boundary, unknown fields are preserved only in raw snapshots, and derived
models reject unexpected fields.

## Taxonomy

Taxonomy is versioned data rather than hard-coded presentation text. Its axes
are:

- `artifact_type`: library, CLI, service, framework, application, plugin,
  template, dataset, or model;
- `capability`: reusable behavior such as authentication, OCR, queueing,
  vector search, testing, observability, or media processing;
- `domain`: developer tools, finance, commerce, media, research, and others;
- `runtime`: browser, server, mobile, embedded, GPU, or multi-platform;
- `interface`: library API, SDK, CLI, REST, GraphQL, MCP, event stream;
- `ecosystem`: language, package manager, and package identity;
- `lifecycle`: active, maintenance, archived, disabled, or unknown;
- `license`: SPDX identifier and compatibility family.

Each taxonomy node has a stable ID, aliases, parents, and inclusion/exclusion
examples. Classifier output never overwrites facts. Later LLM suggestions will
live in a separate proposal layer and require evidence before promotion.

## Data flow and recovery

A discovery run reads its last committed repository ID and requests the next
page. The response is validated, hashed, written atomically, and then inserted
into the state transaction. New repository identities create idempotent work
items. Only after both operations succeed does the durable cursor advance.

Enrichment workers partition work by stable numeric repository ID, not mutable
owner, name, stars, or update time. Each stage has an independent checkpoint so
one broken repository cannot stop global discovery. Retryable failures honor
`Retry-After` and rate-limit reset headers with bounded exponential backoff and
jitter. Permanent deletion, transfer, rename, archival, or transition away from
public visibility creates an observation or tombstone rather than deleting
history.

The state machine is append-oriented: `discovered`, `queued`, `enriched`,
`classified`, `published`, `retry`, or `dead_letter`. A unique processing key
of repository ID, stage, source revision, and analyzer version makes restarts
idempotent. Status output distinguishes completed, pending, retrying, failed,
and dead-letter counts; no command silently labels partial coverage complete.

## Security, licensing, and privacy

GitHub content is untrusted input. Tokens come only from `GITHUB_TOKEN` or the
GitHub Actions secret context and are redacted from exceptions and logs. The
MVP makes only bounded HTTPS requests to configured GitHub API hosts. It does
not run workflows, installers, builds, test suites, or shell instructions from
indexed repositories. Future archive extraction must enforce file-count,
content-size, time, path traversal, symbolic-link, and decompression limits.

Public availability does not grant reuse rights. Every assertion retains the
reported SPDX license and evidence. Missing, unknown, or conflicting licenses
allow discovery and comparison only; they cannot pass the reusable integration
gate. Repository-level licensing is not assumed to override package-level
licenses. The catalog presents provenance and compatibility signals, not legal
advice.

The project stores public technical metadata only. Generated catalogs must
support deletion/tombstone processing for repositories that are removed,
restricted, or subject to a takedown.

## Testing and success criteria

Implementation follows red-green-refactor TDD. Unit tests cover immutable
models, taxonomy matching, stable ordering, cursor planning, retry decisions,
and token redaction. Integration tests use a local mock transport to verify
pagination, atomic cursor advancement, duplicate IDs, interrupted writes,
rate-limit responses, malformed input, and resume behavior. CLI end-to-end
tests run without real network access and compare deterministic exports.

The MVP is acceptable when:

- an interrupted discovery resumes without gaps or duplicate identities;
- the cursor cannot advance before page and queue durability;
- identical facts and versions produce byte-identical catalog output;
- every capability assertion contains evidence and version provenance;
- unknown-license repositories are never marked safe to integrate;
- tokens never appear in state, output, logs, or raised error messages;
- unit, integration, and CLI end-to-end suites pass with at least 80% coverage;
- lint, formatting, type checking, dependency audit, and secret scanning pass;
- the README reports bounded capability and current coverage honestly.

## Deferred work

The MVP does not clone all source code, execute third-party projects, call an
LLM for every repository, perform complete vulnerability scanning, or
automatically assemble and publish a new product. Later phases may add
manifest-level extraction for selected ecosystems, DuckDB/Parquet datasets,
semantic retrieval, license-aware composition planning, and a static web UI.
