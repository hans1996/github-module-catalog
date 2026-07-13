# Taxonomy and classification

Taxonomy v2 turns broad capability labels into a stable, browsable hierarchy. The source of truth
is [`taxonomy.yaml`](../src/github_module_catalog/data/taxonomy.yaml); the current public contract is
taxonomy `2.0.0`, classifier `rules-v2`, and catalog schema `1.1.0`.

The generated [`catalog/taxonomy.md`](../catalog/taxonomy.md) shows the hierarchy and live repository
counts for the current snapshot. Stable IDs are machine contracts. Labels, aliases, examples, and
matching rules can evolve only with versioned, reviewed changes.

## Capability hierarchy

Taxonomy v2 preserves all 11 broad v1 capability IDs and adds 55 focused leaf capabilities. Nine
IDs are roots; `auth` is nested under `security`, and `observability` is nested under `devops`:

| Family or broad branch | Fine-grained capabilities |
| --- | --- |
| `cli` | terminal UI, terminal emulators, shell tooling, package managers |
| `web-ui` | component libraries, dashboards, static-site generators, content management |
| `api-backend` | REST, GraphQL, RPC, realtime APIs, API gateways |
| `auth` | OAuth/OIDC, identity providers, access control, multi-factor authentication |
| `database-storage` | relational, document, key-value, vector and object stores, search engines |
| `ai-ml` | LLM runtimes, agent frameworks, RAG, training, vision, speech AI |
| `testing` | unit, browser E2E, API and performance testing |
| `devops` | CI/CD, containers, Kubernetes, infrastructure and configuration automation |
| `observability` | metrics, logs, tracing, error tracking, profiling |
| `media` | image, video and audio processing, streaming, downloaders |
| `security` | vulnerability scanning, pentesting, cryptography, secrets, network security, reverse engineering, malware analysis |

Some nodes have more than one parent. For example, `computer-vision` and `speech-ai` belong to both
`ai-ml` and `media`; `auth` is also a child of `security`.

## Parent closure and provenance

A direct leaf match automatically emits all ancestors. A repository classified as `oauth-oidc`
therefore also receives `auth` and `security`. Derived parent assertions retain the leaf evidence and
add `taxonomy: derived-from:<leaf-id>`, so every broad count can be traced to its strongest direct
match. A direct parent match wins over a derived one.

This closure keeps the homepage compact: it shows only top-level families, while the generated
taxonomy page exposes children and grandchildren with live counts.

## Deterministic rules, not AI

The current classifier does not use an LLM or AI model. It evaluates validated GitHub metadata with
versioned, deterministic rules:

- exact normalized topics;
- normalized description tokens and phrases;
- resource-only exclusions for tutorials, courses, curated lists, starter kits, and samples;
- leaf-specific exclusions for clients, scanners, VPNs, and dependency-only signals;
- primary-language hints as supporting evidence only;
- lifecycle and observed SPDX metadata for reuse status.

A language hint can raise confidence but can never create a capability by itself. The rules favor
precision: using PostgreSQL does not make an application a relational database engine; using OAuth
does not make it an identity provider; using Playwright does not make it an E2E framework.

Each assertion records the capability ID, taxonomy and classifier versions, confidence, evidence,
source observation hash, license signal, and reuse status. Ordering is deterministic.

## Resource-only filtering

Popular repositories include books, courses, roadmaps, awesome lists, tutorials, and sample apps.
They can be useful references, but they are not reusable implementation modules. Taxonomy v2 checks
explicit resource topics, high-signal name prefixes, and contextual description phrases before any
capability rule runs. The filter deliberately avoids generic words such as `example` and does not
veto a real tool merely because its documentation includes a tutorial. This is deliberately
precision-first: a repository carrying an explicit resource-only topic such as `tutorial` is omitted
from every capability even when other metadata contains a positive signal.

Regression fixtures cover known false positives from the live ranked window, including database
consumers, API clients, AI browser automation, VPN projects, security scanners, learning maps, and
curated lists.

## Selection is separate

Discovery chooses snapshot membership by stars, recent `pushed_at`, public visibility, non-archived
state, and non-fork status. These filters do not create capability labels and are not quality or
security endorsements. Classification runs only after source facts have been validated.

## Reuse and license gate

Classification and reuse are separate decisions. A capability may be present while reuse remains
blocked. Missing licenses, `NOASSERTION`, unrecognized or non-permissive SPDX values, and archived or
disabled lifecycle states remain `discovery_only`. `safe_to_integrate` is a conservative project
policy signal, not legal advice or a security audit.

## Changing the taxonomy

1. Keep IDs lowercase, stable, and linked to valid parents.
2. Add inclusion and exclusion examples for every node.
3. Add positive signals and real false-positive regression fixtures before changing rules.
4. Increment the taxonomy or classifier version when semantics change.
5. Run formatting, lint, type checks, the complete test suite, and offline reclassification against
   the current ranked catalog.
6. Rebuild and validate the tracked JSON, YAML, taxonomy page, module pages, and root homepage.

Never execute third-party repository code to classify it. Future semantic enrichment must preserve
analyzer versions, evidence references, confidence, and immutable source-observation hashes.
