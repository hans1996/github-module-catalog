# GitHub Module Catalog

**A ranked, machine-readable catalog of popular, recently pushed public GitHub repositories, organized by capability.**

[![CI](https://github.com/hans1996/github-module-catalog/actions/workflows/ci.yml/badge.svg)](https://github.com/hans1996/github-module-catalog/actions/workflows/ci.yml)
[![Catalog refresh](https://github.com/hans1996/github-module-catalog/actions/workflows/discover.yml/badge.svg)](https://github.com/hans1996/github-module-catalog/actions/workflows/discover.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-2563eb.svg)](LICENSE)

Discover existing building blocks before rebuilding them. Use the catalog to shortlist libraries,
CLIs, services, plugins, and templates for larger systems.

[Browse the catalog](catalog/README.md) · [Explore Taxonomy v2](catalog/taxonomy.md) · [Use the JSON index](catalog/catalog.json)

<!-- catalog-index:begin -->

## Live catalog

| Indexed repositories | GitHub Search matches | Last refresh |
| ---: | ---: | --- |
| **1,000** | **181,459** | **2026-07-13 11:29 UTC** |

**Selection:** **100+ stars** · pushed since **2025-07-13** · public · non-archived · non-fork

**Ranking:** stars descending, then repository ID. This is a top-ranked window, not an exhaustive index of GitHub.

[Browse the full catalog](catalog/README.md) · [Explore Taxonomy v2](catalog/taxonomy.md) · [JSON](catalog/catalog.json) · [YAML](catalog/catalog.yaml)

### Capability families

Capability families overlap; one repository may appear in more than one family.

| Family | Repositories | Direct subcategories |
| --- | ---: | ---: |
| [`ai-ml`](catalog/modules/ai-ml.md) | 211 | 6 |
| [`api-backend`](catalog/modules/api-backend.md) | 58 | 5 |
| [`cli`](catalog/modules/cli.md) | 95 | 4 |
| [`database-storage`](catalog/modules/database-storage.md) | 43 | 6 |
| [`devops`](catalog/modules/devops.md) | 46 | 6 |
| [`media`](catalog/modules/media.md) | 62 | 7 |
| [`security`](catalog/modules/security.md) | 41 | 8 |
| [`testing`](catalog/modules/testing.md) | 11 | 4 |
| [`web-ui`](catalog/modules/web-ui.md) | 62 | 4 |
<!-- catalog-index:end -->

## How it works

1. **Discover** — query GitHub Search for popular repositories with a recent push.
2. **Validate** — recheck selection facts, schema, ordering, and artifact integrity.
3. **Classify and publish** — map repository metadata to capabilities and commit the snapshot here.

The crawler reads metadata only. It does not clone repositories or execute third-party code.

## Selection policy

| Signal | Default |
| --- | --- |
| Popularity | 100+ stars |
| Recency | Pushed within the last 365 days |
| Scope | Public, non-archived, non-fork repositories |
| Ranking | Stars descending, then GitHub repository ID |
| Window | Top 1,000 results from one GitHub Search query |
| Refresh | Scheduled every 6 hours. Each run rebuilds and replaces the snapshot. |

GitHub Search exposes at most 1,000 results for a query, so scheduled runs refresh the current
top-ranked window instead of accumulating another 1,000 repositories. See the
[GitHub Search API limits](https://docs.github.com/en/rest/search/search).

## Use the data

- **Browse:** [`catalog/README.md`](catalog/README.md)
- **Explore the hierarchy:** [`catalog/taxonomy.md`](catalog/taxonomy.md)
- **Build tools:** [`catalog/catalog.json`](catalog/catalog.json) or [`catalog/catalog.yaml`](catalog/catalog.yaml)
- **Find candidates by capability:** [`catalog/modules/`](catalog/modules/)

For local commands and automation details, read the [operations guide](docs/operations.md).
For classification and license semantics, read the [taxonomy guide](docs/taxonomy.md).

## Scope and trust

- Popularity and recent pushes are discovery signals, not a quality or security endorsement.
- Public does not mean reusable; verify each repository's license and lifecycle before integration.
- Capability labels come from deterministic metadata rules, not a source-code or legal audit.

## Contributing

[Issues](https://github.com/hans1996/github-module-catalog/issues) and
[pull requests](https://github.com/hans1996/github-module-catalog/pulls) are welcome for taxonomy
rules, discovery policy, and export formats.
If the catalog saves you time, consider starring the repository to follow its progress.

## License

The catalog software is released under the [MIT License](LICENSE). Third-party repositories retain
their own rights and license terms.
