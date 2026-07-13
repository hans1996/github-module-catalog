# GitHub Module Catalog MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Build and publish a tested Python CLI that resumably discovers public GitHub repositories, classifies their reusable capabilities with provenance and license gates, and exports deterministic JSON, YAML, and Markdown catalogs.

**Architecture:** A GitHub REST adapter writes immutable raw pages and advances a SQLite cursor only after durable storage. Domain services convert validated observations into versioned capability assertions, while deterministic exporters create human- and machine-readable catalogs. Public-repository discovery uses the monotonic `GET /repositories?since=<id>` feed; repository search is never used as a completeness source.

**Tech Stack:** Python 3.12, Typer, Pydantic v2, HTTPX, PyYAML, SQLite, pytest, respx, pytest-cov, Ruff, mypy, pip-audit.

---

### Task 1: Project scaffold and quality gates

**Files:**
- Create: `pyproject.toml`
- Create: `src/github_module_catalog/__init__.py`
- Create: `tests/test_package.py`
- Create: `.github/workflows/ci.yml`

**Step 1: Write the failing package test**

```python
from github_module_catalog import __version__


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"
```

**Step 2: Run test to verify RED**

Run: `uv run pytest tests/test_package.py -q`
Expected: collection fails because `github_module_catalog` does not exist.

**Step 3: Add the minimal package and configuration**

Configure a `src` layout, `ghmod` console script, Python `>=3.12`, runtime
dependencies (`httpx`, `pydantic`, `PyYAML`, `typer`) and development tools.
Set Ruff and mypy strict mode. Keep the coverage threshold at 80% in coverage
configuration, while CI and full verification invoke pytest-cov explicitly so
targeted pytest commands remain independently runnable. CI runs format check,
lint, type check, tests with coverage, and dependency audit without live network
tests.

**Step 4: Run GREEN and quality checks**

Run: `uv sync --all-groups`
Run: `uv run pytest tests/test_package.py -q`
Expected: one passing test.

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock src tests .github/workflows/ci.yml
git commit -m "chore: scaffold github module catalog"
```

### Task 2: Immutable domain models and taxonomy classifier

**Files:**
- Create: `src/github_module_catalog/models.py`
- Create: `src/github_module_catalog/taxonomy.py`
- Create: `config/taxonomy.yaml`
- Create: `tests/unit/test_models.py`
- Create: `tests/unit/test_taxonomy.py`

**Step 1: Write failing model tests**

Cover these behaviors:

```python
def test_repository_observation_is_immutable() -> None:
    observation = repository_fixture()
    with pytest.raises(ValidationError):
        observation.name = "changed"


def test_unknown_license_is_not_reusable() -> None:
    observation = repository_fixture(license_spdx=None)
    assert observation.reuse_status == ReuseStatus.DISCOVERY_ONLY
```

Also test that unknown input fields are rejected; URLs, timestamps, numeric IDs,
topics, lifecycle flags, and optional license data validate; and serialized
output is stable.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_taxonomy.py -q`
Expected: import failure for missing model and taxonomy modules.

**Step 3: Implement minimal immutable models**

Use Pydantic `ConfigDict(frozen=True, extra="forbid")`. Define:

- `RepositoryIdentity`
- `RepositoryObservation`
- `CapabilityAssertion`
- `CatalogEntry`
- `CatalogManifest`
- `ReuseStatus`
- `Evidence`

Repository numeric ID is the identity key. Assertions contain taxonomy version,
classifier version, confidence, evidence, source observation hash, and license
reuse status. No method mutates an instance.

Load a small v1 YAML taxonomy with stable IDs and axes. Classify by normalized
topics, description tokens, language, archived state, and license. Multiple
assertions per repository are allowed. At minimum include `cli`, `web-ui`,
`api-backend`, `auth`, `database-storage`, `ai-ml`, `testing`, `devops`,
`observability`, `media`, and `security` capabilities.

**Step 4: Run GREEN and full unit tests**

Run: `uv run pytest tests/unit/test_models.py tests/unit/test_taxonomy.py -q`
Expected: all tests pass.

**Step 5: Commit**

```bash
git add config src/github_module_catalog/models.py src/github_module_catalog/taxonomy.py tests/unit
git commit -m "feat: add immutable capability catalog models"
```

### Task 3: GitHub discovery source and rate-limit behavior

**Files:**
- Create: `src/github_module_catalog/source.py`
- Create: `src/github_module_catalog/github.py`
- Create: `tests/integration/test_github_source.py`

**Step 1: Write failing HTTP adapter tests**

Use `httpx.MockTransport`, not a live API. Verify:

- request path is `/repositories`, `since` is the durable cursor, and the
  current supported GitHub API version header is present;
- `Link: <...>; rel="next"` controls pagination rather than a hard-coded page
  count;
- inventory records omit enrichment-only fields safely;
- `403`/`429` parse `Retry-After` before `X-RateLimit-Reset`;
- `304` creates an unchanged result;
- malformed JSON and invalid records fail closed;
- authorization values never appear in exceptions or object representations.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/integration/test_github_source.py -q`
Expected: import failure for the missing GitHub source.

**Step 3: Implement the source protocol and adapter**

Define a `RepositorySource` protocol and a `GitHubRepositorySource` with an
injected `httpx.Client` or transport. Token comes only from a constructor value
provided by the environment boundary. Requests are sequential, time-bounded,
and restricted to an allowlisted HTTPS API base URL. Return frozen page/result
models containing the raw bytes hash, ETag, next cursor/URL, rate-limit facts,
and validated minimal repository identities.

Do not sleep inside the adapter. Return a typed retry decision so the pipeline
or scheduler owns waiting. Never log request headers.

**Step 4: Run GREEN**

Run: `uv run pytest tests/integration/test_github_source.py -q`
Expected: all HTTP adapter tests pass.

**Step 5: Commit**

```bash
git add src/github_module_catalog/source.py src/github_module_catalog/github.py tests/integration/test_github_source.py
git commit -m "feat: add resilient github discovery source"
```

### Task 4: Crash-safe raw storage and SQLite checkpoints

**Files:**
- Create: `src/github_module_catalog/storage.py`
- Create: `src/github_module_catalog/state.py`
- Create: `tests/integration/test_state_store.py`

**Step 1: Write failing durability tests**

Verify:

- raw response bytes are written to a content-addressed SHA-256 path through a
  temporary file and atomic rename;
- page metadata and repository identities commit in one SQLite transaction;
- the cursor advances only after the raw object exists and the transaction
  commits;
- duplicate GitHub repository IDs are idempotent;
- an injected failure before commit leaves the previous cursor unchanged;
- state contains no token or authorization header;
- independent stage checkpoints and append-only work-item events are retained.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/integration/test_state_store.py -q`
Expected: import failure for missing storage/state modules.

**Step 3: Implement minimal stores**

Use SQLite foreign keys and explicit transactions. The schema has crawl runs,
discovery pages, repository identities, observations, work-item events, stage
checkpoints, and catalog publications. Raw storage writes under
`data/raw/sha256/<prefix>/<digest>.json`. Runtime paths are configurable.

Expose repository methods that return new frozen model objects or scalar
results. Never return mutable internal state.

**Step 4: Run GREEN**

Run: `uv run pytest tests/integration/test_state_store.py -q`
Expected: all durability and idempotency tests pass.

**Step 5: Commit**

```bash
git add src/github_module_catalog/storage.py src/github_module_catalog/state.py tests/integration/test_state_store.py
git commit -m "feat: add crash safe catalog checkpoints"
```

### Task 5: Discovery pipeline and deterministic exporters

**Files:**
- Create: `src/github_module_catalog/scanner.py`
- Create: `src/github_module_catalog/catalog.py`
- Create: `src/github_module_catalog/exporters.py`
- Create: `tests/integration/test_scanner.py`
- Create: `tests/unit/test_exporters.py`

**Step 1: Write failing pipeline/export tests**

Verify:

- scan resumes at the committed numeric cursor;
- an interrupted page is fetched again and cannot create duplicate identities;
- discovery continues while enrichment failures remain isolated;
- repositories are classified from validated observations only;
- JSON/YAML/Markdown contain equivalent entries sorted by repository numeric ID
  and capability stable ID;
- two builds from identical inputs are byte-for-byte equal;
- manifest reports cursor range, counts, pending/retry/dead-letter states,
  versions, and source hashes;
- unknown-license entries display `discovery_only`, never `safe_to_integrate`.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/integration/test_scanner.py tests/unit/test_exporters.py -q`
Expected: imports fail for missing pipeline/export modules.

**Step 3: Implement the pipeline**

Keep orchestration functions under 50 lines by composing source, raw storage,
state, classifier, and exporter interfaces. Export `catalog.json`,
`catalog.yaml`, `README.md`, module-specific Markdown pages, and
`manifest.json`. Use canonical JSON formatting and stable YAML options. Dynamic
run time appears in the manifest only when explicitly supplied by the caller.

**Step 4: Run GREEN**

Run: `uv run pytest tests/integration/test_scanner.py tests/unit/test_exporters.py -q`
Expected: all pipeline and deterministic export tests pass.

**Step 5: Commit**

```bash
git add src/github_module_catalog/scanner.py src/github_module_catalog/catalog.py src/github_module_catalog/exporters.py tests/integration/test_scanner.py tests/unit/test_exporters.py
git commit -m "feat: build resumable deterministic catalogs"
```

### Task 6: CLI, bounded scheduled workflow, and operations docs

**Files:**
- Create: `src/github_module_catalog/cli.py`
- Create: `tests/e2e/test_cli.py`
- Create: `.github/workflows/discover.yml`
- Create: `.env.example`
- Create: `docs/operations.md`
- Create: `docs/taxonomy.md`
- Modify: `README.md`

**Step 1: Write failing CLI tests**

Use Typer's test runner with injected fake sources. Cover:

- `ghmod init --workspace PATH`
- `ghmod discover --max-pages N`
- `ghmod status`
- `ghmod classify`
- `ghmod build --format json --format yaml --format markdown`
- `ghmod validate`

Commands return non-zero on invalid state, missing token for live discovery,
unsafe base URL, and schema validation errors. Tests must never call the real
network.

**Step 2: Run tests to verify RED**

Run: `uv run pytest tests/e2e/test_cli.py -q`
Expected: import failure or missing CLI commands.

**Step 3: Implement CLI and workflows**

Read `GITHUB_TOKEN` only at the CLI boundary. Default discovery is bounded by a
required page or time budget. Scheduled workflow runs at a non-zero minute,
supports `workflow_dispatch`, uses minimal permissions, has concurrency
control, and uploads state/output artifacts without committing datasets.

Document credential setup, API budgets, cursor semantics, recovery, known
coverage limits, licensing gates, and how a future GitHub App replaces a user
token.

**Step 4: Run GREEN and all quality gates**

Run: `uv run pytest -q --cov=github_module_catalog --cov-report=term-missing --cov-fail-under=80`
Run: `uv run ruff format --check .`
Run: `uv run ruff check .`
Run: `uv run mypy src tests`
Run: `uv run pip-audit`
Expected: all commands pass without warnings or leaked credentials.

**Step 5: Commit**

```bash
git add src/github_module_catalog/cli.py tests/e2e .github/workflows/discover.yml .env.example docs README.md
git commit -m "feat: expose catalog operations through cli"
```

### Task 7: Final verification and public repository publication

**Files:**
- Modify only files required by review findings.

**Step 1: Generate a local sample without live GitHub calls**

Run the CLI against test fixtures and validate every output schema.

**Step 2: Run complete verification**

Run the test, coverage, format, lint, type, dependency-audit, secret-scan, and
`git diff --check` commands. Review the full branch diff for security and scope.

**Step 3: Review**

Obtain specification-compliance, code-quality, and security reviews. Resolve all
critical and high findings and re-run verification.

**Step 4: Integrate and publish**

Fast-forward or merge `feat/mvp` into local `main`, create public repository
`hans1996/github-module-catalog`, add topics, push `main`, and verify the remote
default branch and Actions configuration.

**Step 5: Record publication evidence**

Report the repository URL, commit SHA, test counts, coverage, current discovery
cursor/sample counts, and explicitly deferred work.
