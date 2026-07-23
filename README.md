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
| **1,000** | **182,467** | **2026-07-23 19:49 UTC** |

**Selection:** **100+ stars** · pushed since **2025-07-23** · public · non-archived · non-fork

**Ranking:** stars descending, then repository ID. This is a top-ranked window, not an exhaustive index of GitHub.

[Browse the full catalog](catalog/README.md) · [Explore Taxonomy v2](catalog/taxonomy.md) · [JSON](catalog/catalog.json) · [YAML](catalog/catalog.yaml)

### Capability families

Capability families overlap; one repository may appear in more than one family.

| Family | Repositories | Fine-grained capability index |
| --- | ---: | --- |
| [`ai-ml`](catalog/modules/ai-ml.md) | 215 | [`ai-agent-framework`](catalog/modules/ai-agent-framework.md) (5) · [`computer-vision`](catalog/modules/computer-vision.md) (19) · [`llm-runtime`](catalog/modules/llm-runtime.md) (3) · [`model-training`](catalog/modules/model-training.md) (4) · [`rag-retrieval`](catalog/modules/rag-retrieval.md) (25) · [`speech-ai`](catalog/modules/speech-ai.md) (13) |
| [`api-backend`](catalog/modules/api-backend.md) | 58 | [`api-gateway`](catalog/modules/api-gateway.md) (5) · [`graphql-api`](catalog/modules/graphql-api.md) (1) · [`realtime-api`](catalog/modules/realtime-api.md) (1) · [`rest-api`](catalog/modules/rest-api.md) (7) · [`rpc-api`](catalog/modules/rpc-api.md) (2) |
| [`cli`](catalog/modules/cli.md) | 94 | [`package-manager`](catalog/modules/package-manager.md) (6) · [`shell-tooling`](catalog/modules/shell-tooling.md) (23) · [`terminal-emulator`](catalog/modules/terminal-emulator.md) (7) · [`terminal-ui`](catalog/modules/terminal-ui.md) (8) |
| [`database-storage`](catalog/modules/database-storage.md) | 43 | [`cache-key-value`](catalog/modules/cache-key-value.md) (4) · [`document-database`](catalog/modules/document-database.md) (1) · [`object-storage`](catalog/modules/object-storage.md) (2) · [`relational-database`](catalog/modules/relational-database.md) (2) · [`search-engine`](catalog/modules/search-engine.md) (5) · [`vector-database`](catalog/modules/vector-database.md) (9) |
| [`devops`](catalog/modules/devops.md) | 46 | [`ci-cd`](catalog/modules/ci-cd.md) (3) · [`configuration-management`](catalog/modules/configuration-management.md) (3) · [`container-tooling`](catalog/modules/container-tooling.md) (4) · [`infrastructure-as-code`](catalog/modules/infrastructure-as-code.md) (1) · [`kubernetes-tooling`](catalog/modules/kubernetes-tooling.md) (2) · [`observability`](catalog/modules/observability.md) (19) · [`distributed-tracing`](catalog/modules/distributed-tracing.md) (1) · [`error-tracking`](catalog/modules/error-tracking.md) (2) · [`log-management`](catalog/modules/log-management.md) (1) · `metrics-monitoring` (0) · [`profiling`](catalog/modules/profiling.md) (2) |
| [`media`](catalog/modules/media.md) | 62 | [`audio-processing`](catalog/modules/audio-processing.md) (2) · [`computer-vision`](catalog/modules/computer-vision.md) (19) · [`image-processing`](catalog/modules/image-processing.md) (6) · [`media-downloader`](catalog/modules/media-downloader.md) (2) · [`media-streaming`](catalog/modules/media-streaming.md) (3) · [`speech-ai`](catalog/modules/speech-ai.md) (13) · [`video-processing`](catalog/modules/video-processing.md) (10) |
| [`security`](catalog/modules/security.md) | 41 | [`auth`](catalog/modules/auth.md) (9) · [`cryptography`](catalog/modules/cryptography.md) (3) · [`malware-analysis`](catalog/modules/malware-analysis.md) (1) · [`network-security`](catalog/modules/network-security.md) (1) · [`penetration-testing`](catalog/modules/penetration-testing.md) (7) · [`reverse-engineering`](catalog/modules/reverse-engineering.md) (5) · [`secrets-management`](catalog/modules/secrets-management.md) (1) · [`vulnerability-scanning`](catalog/modules/vulnerability-scanning.md) (2) · [`access-control`](catalog/modules/access-control.md) (1) · [`identity-provider`](catalog/modules/identity-provider.md) (1) · `multi-factor-auth` (0) · `oauth-oidc` (0) |
| [`testing`](catalog/modules/testing.md) | 11 | [`api-testing`](catalog/modules/api-testing.md) (2) · [`browser-e2e-testing`](catalog/modules/browser-e2e-testing.md) (2) · [`performance-testing`](catalog/modules/performance-testing.md) (1) · [`unit-test-framework`](catalog/modules/unit-test-framework.md) (1) |
| [`web-ui`](catalog/modules/web-ui.md) | 62 | [`content-management`](catalog/modules/content-management.md) (7) · [`dashboard-ui`](catalog/modules/dashboard-ui.md) (6) · [`static-site-generator`](catalog/modules/static-site-generator.md) (7) · [`ui-component-library`](catalog/modules/ui-component-library.md) (15) |
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
