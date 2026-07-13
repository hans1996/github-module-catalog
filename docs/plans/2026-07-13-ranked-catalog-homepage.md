# Ranked Catalog Homepage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the scheduled exhaustive-feed catalog with a ranked, recently maintained GitHub repository snapshot and publish it directly to the repository homepage.

**Architecture:** Add a separate Search API snapshot adapter instead of weakening the existing repository-ID cursor invariants. Build one all-or-nothing catalog from validated Search pages, then pass only validated output to a least-privilege publishing job that updates tracked `catalog/` files and a managed root README section.

**Tech Stack:** Python 3.12, Pydantic, httpx, Typer, pytest, GitHub Actions, GitHub REST Search API.

### Task 1: Add ranked source facts and manifest policy

**Files:**
- Modify: `src/github_module_catalog/models.py`
- Modify: `src/github_module_catalog/catalog.py`
- Modify: `src/github_module_catalog/exporters.py`
- Test: `tests/unit/test_models.py`
- Test: `tests/unit/test_exporters.py`

1. Write failing tests for `stargazers_count`, selection criteria, stable rank,
   eligibility validation, and stars-desc Markdown output.
2. Run the focused tests and verify failures are caused by missing fields.
3. Add immutable criteria/rank fields and deterministic `(-stars, id)` ordering.
4. Render rank, stars, last push, criteria, and honest Search coverage.
5. Run focused tests and refactor only while green.

### Task 2: Implement the ranked GitHub Search snapshot

**Files:**
- Create: `src/github_module_catalog/github_search.py`
- Create: `tests/integration/test_github_search.py`

1. Write failing tests for the canonical query, fixed UTC cutoff, Search response
   envelope, eligibility revalidation, incomplete-result rejection, bounded
   bodies, pagination, deduplication, raw hashes, and deterministic ties.
2. Run the focused tests and confirm RED.
3. Implement `GitHubSearchCriteria`, the hardened Search adapter, and an
   all-or-nothing snapshot collector capped at ten pages.
4. Run focused tests and refactor while green.

### Task 3: Add a ranked refresh CLI lifecycle

**Files:**
- Modify: `src/github_module_catalog/cli.py`
- Modify: `tests/e2e/test_cli.py`

1. Write a failing CLI test for `refresh --min-stars 100
   --active-within-days 365 --max-pages 10` using a fake ranked source.
2. Verify the test fails because the command is absent.
3. Build and atomically publish the ranked snapshot to
   `<workspace>/catalog-output`; keep the existing exhaustive commands intact.
4. Add output-only validation and verify malformed or incomplete snapshots do
   not replace a valid publication.
5. Run CLI and integration tests.

### Task 4: Publish validated output into the repository

**Files:**
- Create: `scripts/publish_catalog.py`
- Create: `tests/unit/test_repository_publication.py`
- Modify: `README.md`

1. Write failing tests for manifest verification, exact file sets, digest
   mismatches, path traversal, symlinks, stale module removal, README marker
   preservation, duplicate markers, malicious metadata, and idempotence.
2. Verify RED.
3. Implement a standard-library-only publisher using staging and atomic
   replacement. Generate the homepage section only from typed safe fields.
4. Run focused tests and confirm GREEN.

### Task 5: Change the scheduled workflow and documentation

**Files:**
- Modify: `.github/workflows/discover.yml`
- Modify: `tests/unit/test_workflows.py`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `docs/taxonomy.md`

1. Write failing workflow contract tests for ranked inputs, read-only discovery,
   isolated write-capable publication, pinned actions, restricted staging, and
   non-force push.
2. Verify RED.
3. Implement the two-job workflow and update documentation to distinguish
   ranked discovery from capability classification.
4. Run workflow tests, formatting, lint, and type checking.

### Task 6: Publish and verify a real snapshot

**Files:**
- Create/update: `catalog/**`
- Update: `README.md` managed section

1. Run a real authenticated ranked refresh with the default criteria.
2. Validate and publish it into the feature worktree.
3. Run the complete test, coverage, lint, format, type, build, and security
   suite.
4. Request code and security review, fix all high-confidence findings, commit,
   merge, and push.
5. Trigger the workflow and verify the repository homepage and tracked catalog
   on GitHub.
