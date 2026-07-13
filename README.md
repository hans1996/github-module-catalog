# GitHub Module Catalog

Turn public GitHub repositories into a traceable catalog of reusable software
capabilities.

GitHub Module Catalog is designed to answer questions such as:

- Does an existing project already provide this capability?
- Which repositories expose reusable libraries, CLIs, services, plugins, or
  templates for it?
- Is the component active, licensed, and compatible with the intended use?
- Which smaller modules could be combined into a larger system?

The long-term goal is broad discovery of public GitHub repositories. The
catalog advances incrementally: it records an ordered discovery cursor,
preserves immutable source observations, and publishes measurable coverage
instead of claiming that a partial search is "all of GitHub."

## 中文願景

這個專案會逐步整理 GitHub 公開 repositories，將 repository 視為模組的來源容器，
再把其中可重用的功能整理成有分類、證據、版本與授權狀態的 capability catalog。
未來建立新專案時，可以先搜尋既有能力，避免重複造輪子，並安全地組合成更大的系統。

## Core principles

1. **Repository is not module.** A repository can provide many capabilities;
   a capability can have implementations in many repositories.
2. **Complete discovery has a cursor.** Public repository discovery uses the
   ordered GitHub repository feed; repository search only prioritizes deeper
   analysis.
3. **Every conclusion has evidence.** Classification keeps source facts,
   analyzer version, taxonomy version, confidence, and evidence references.
4. **Public does not mean reusable.** Unknown or incompatible licenses remain
   discoverable but are never presented as safe to integrate.
5. **Untrusted projects are data.** The crawler never executes repository
   scripts, workflows, builds, or instructions.
6. **Generated datasets do not live in Git.** The repository stores code,
   schemas, taxonomy, fixtures, and small samples; large catalogs are published
   as versioned artifacts.

## MVP

The first release will provide:

- resumable discovery through `GET /repositories?since=<repository-id>`;
- immutable repository observations keyed by GitHub numeric repository ID;
- deterministic, multi-axis capability classification;
- license and provenance fields on every reusable-module assertion;
- JSON, YAML, and Markdown exports with byte-stable ordering;
- a CLI for discovery, status, classification, validation, and export;
- unit, integration, and CLI end-to-end tests with at least 80% coverage;
- GitHub Actions for CI and bounded scheduled discovery.

See [the approved architecture](docs/plans/2026-07-13-github-module-catalog-design.md)
for the complete data flow and safety boundaries.

## Quick start

```bash
uv sync --all-groups --locked
uv run ghmod init --workspace .local/catalog
export GITHUB_TOKEN="$(gh auth token)"
uv run ghmod discover --workspace .local/catalog --max-pages 10
uv run ghmod status --workspace .local/catalog
uv run ghmod classify --workspace .local/catalog
uv run ghmod build --workspace .local/catalog
uv run ghmod validate --workspace .local/catalog
```

The token is read only by `discover`; do not print it or commit it to a file.
See the [operations guide](docs/operations.md) for cursor recovery, API budgets,
scheduled runs, security boundaries, and GitHub App migration. See the
[taxonomy guide](docs/taxonomy.md) for classification and licensing semantics.

## Status

The MVP implements bounded public-repository discovery, durable cursors and raw
objects, initial observations from complete inventory metadata, deterministic
classification, JSON/YAML/Markdown publication, integrity validation, and a
scheduled GitHub Actions workflow. Coverage is incremental and explicitly
measured; the current catalog must not be described as all GitHub projects.
Identity-only repositories remain queued for deferred enrichment.

## License

The catalog software is available under the MIT License. Metadata, source
code, names, and other material obtained from third-party repositories retain
their original rights and licenses.
