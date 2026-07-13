# Taxonomy and classification

The versioned source of truth is
[`src/github_module_catalog/data/taxonomy.yaml`](../src/github_module_catalog/data/taxonomy.yaml).
Stable node IDs are machine contracts; labels, aliases, examples, and rules may
be clarified only with an explicit taxonomy version change when meaning shifts.

## Axes

The MVP defines independent axes for artifact type, capability, domain,
runtime, interface, ecosystem, lifecycle, and license. A repository is a source
container, not a single module. One repository may therefore yield several
capability assertions, and many repositories may implement the same capability.

Initial capability IDs include `cli`, `web-ui`, `api-backend`, `auth`,
`database-storage`, `ai-ml`, `testing`, `devops`, `observability`, `media`, and
`security`.

## Deterministic evidence

The rules classifier considers only validated observation facts:

- normalized GitHub topics;
- normalized description tokens;
- primary-language hints;
- archived/disabled lifecycle flags;
- observed SPDX license metadata.

Each assertion records the capability ID, taxonomy and classifier versions,
confidence, matched evidence, source observation hash, license signal, and reuse
status. Output order is deterministic by numeric repository ID and stable
capability ID. Classification failure is isolated to one repository and is
reported in `classification_failure_repository_ids`; it does not discard other
entries.

Language is supporting evidence only. A Python repository is not classified as
a CLI merely because it is written in Python. At least a configured topic or
description signal must match, and exclusion tokens can veto an otherwise
ambiguous rule.

## License gate

Classification and reuse are separate decisions. Capability evidence may be
present while reuse remains blocked. Missing license metadata, `NOASSERTION`,
non-permissive or unrecognized SPDX values, and archived or disabled lifecycle
states remain `discovery_only`. The gate is deliberately conservative and is
not legal advice.

## Changing the taxonomy

1. edit the YAML source and keep all IDs lowercase and stable;
2. add inclusion and exclusion examples for every new node;
3. add deterministic rule fixtures for positive, negative, lifecycle, and
   license cases;
4. increment the taxonomy version when semantics change;
5. run unit tests, build a catalog, and run `ghmod validate` before publishing.

Do not classify from repository code execution, generated summaries without
provenance, mutable global state, or unvalidated API fields. Future semantic
enrichment must retain evidence references, analyzer versions, confidence, and
the immutable source-observation hash.
