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

## Status

Initial implementation is in progress.

## License

The catalog software is available under the MIT License. Metadata, source
code, names, and other material obtained from third-party repositories retain
their original rights and licenses.

