# Taxonomy v2 design

## Goal

Taxonomy v2 turns the current eleven broad capability labels into a stable,
hierarchical catalog that is useful for choosing reusable building blocks. It
must increase precision and browseability without breaking consumers that rely
on the existing capability IDs or assertion shape.

## Decisions

- Keep the existing eleven broad capability IDs stable. Nine remain roots;
  `auth` stays under `security`, and `observability` stays under `devops`.
- Add a first wave of high-signal leaf capabilities beneath those parents.
- Infer every ancestor when a leaf matches, so existing broad-category queries
  continue to work.
- Keep classification deterministic and metadata-only. Taxonomy v2 does not
  call an LLM, clone repositories, read third-party instructions, or execute
  third-party code.
- Add topic-level exclusion signals and exact normalized description phrases to
  reduce curated-list, tutorial, client-SDK, and other false positives.
- Publish a compact hierarchy snapshot with each catalog and generate a
  dedicated `catalog/taxonomy.md` browse page. The root README stays compact by
  showing top-level groups and linking to the detailed tree.
- Bump taxonomy to `2.0.0`, classifier to `rules-v2`, and the additive catalog
  schema to `1.1.0`.

General multi-axis assertions are deliberately deferred. The existing YAML
continues to define artifact, domain, runtime, interface, ecosystem, lifecycle,
and license axes, but changing the public assertion model from capability-only
to arbitrary axes requires a separate catalog schema 2.0 migration.

## Capability hierarchy

The stable broad IDs remain `cli`, `web-ui`, `api-backend`, `auth`,
`database-storage`, `ai-ml`, `testing`, `devops`, `observability`, `media`, and
`security`. The first v2 leaf set covers high-signal module types such as
terminal UIs, component libraries, API gateways, OAuth/OIDC, vector databases,
LLM runtimes, agent frameworks, browser E2E testing, infrastructure as code,
distributed tracing, media processing, and vulnerability scanning.

Nodes may have multiple parents. For example, computer-vision and speech-AI
components belong to both `ai-ml` and `media`; `auth` remains a child of
`security`. Parent graphs must be closed within one axis and acyclic.

## Matching and provenance

Each rule may use:

- exact normalized GitHub topics;
- exact normalized description tokens;
- normalized multi-word description phrases;
- primary-language hints that only increase confidence after a positive match;
- description-token and topic exclusions that veto the rule.

Direct rule evidence wins over inferred evidence. If an ancestor has no direct
match, the strongest deterministic child match supplies its evidence and
confidence, plus a `taxonomy` evidence item identifying the child-to-parent
derivation. Ties are resolved by stable node ID. A repository receives at most
one assertion per capability ID, sorted by capability ID.

Selection remains independent: star count, recent push, visibility, archived
state, and fork state decide membership before classification. License and
lifecycle continue to control reuse status rather than capability membership.

## Publication contract

`CatalogManifest.capability_definitions` contains the public, sorted capability
ID, label, and parent IDs used for that snapshot. It is additive, so existing
assertion consumers can ignore it. Exporters produce:

- the existing JSON and YAML catalogs;
- the existing full catalog README;
- one module page for every capability present in the snapshot;
- a new `taxonomy.md` hierarchy page;
- the existing integrity manifest with the new artifact included.

The safe repository publisher validates node IDs, parents, cycles, assertion
targets, module pages, and artifact hashes before it updates tracked files. The
homepage renders only top-level capability counts plus a link to the detailed
taxonomy page, preventing dozens of leaf links from overwhelming the public
README.

## Failure behavior

- Invalid hierarchy, missing rule targets, cycles, or duplicate IDs fail while
  loading the packaged taxonomy.
- A repository-level classifier exception remains isolated to that repository
  and is recorded in `classification_failure_repository_ids`.
- A malformed hierarchy or artifact causes publication to fail before any
  tracked README or catalog path is changed.
- Unknown or low-signal repositories remain unclassified; v2 does not guess.

## Verification

Tests cover packaged taxonomy structure, every leaf rule, exclusions, phrase
matching, ancestor closure, multi-parent closure, deterministic deduplication,
manifest hierarchy validation, exporter parity, safe publisher rejection, CLI
lifecycle, wheel packaging, formatting, strict typing, 80%+ coverage, secret
scanning, and dependency audit.
