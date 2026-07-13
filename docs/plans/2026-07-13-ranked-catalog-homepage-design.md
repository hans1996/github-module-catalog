# Ranked Catalog Homepage Design

## Goal

Publish a useful, reviewable catalog directly on the GitHub repository homepage.
The scheduled catalog is a ranked snapshot of popular public repositories that
show recent maintenance activity; it is not an exhaustive mirror of GitHub.

## Selection policy

The default ranked query is:

```text
stars:>=100 pushed:>=<run-start-minus-365-days> archived:false is:public
```

The request uses `sort=stars`, `order=desc`, `per_page=100`, and pages 1 through
10. GitHub excludes forks unless `fork:true` or `fork:only` is requested. Every
returned item is nevertheless checked locally for public visibility, at least
the configured star count, a qualifying `pushed_at`, `archived == false`, and
`fork == false`.

The star threshold and activity window are workflow inputs. The UTC cutoff is
fixed once at the beginning of a run. A snapshot records the exact criteria,
API match count, fetched page count, result limit, source-page hashes, stars,
last-push time, and stable rank.

GitHub Search returns at most 1,000 results for one query and has a separate
authenticated rate limit. Therefore the publication describes itself as the
top ranked search window, never as all matching repositories. See the official
[Search REST API](https://docs.github.com/en/rest/search/search) and
[repository qualifiers](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories).

## Snapshot data flow

The existing `GET /repositories?since=<id>` adapter and its repository-ID
cursor remain intact as an optional exhaustive-feed primitive. Ranked discovery
uses a separate adapter and orchestration path because Search pagination is a
dynamic page-ranked view, not a monotonic repository-ID feed.

Each scheduled run starts ranked discovery at page 1 and fetches all requested
pages in the same run. It validates the Search response envelope, rejects
`incomplete_results=true`, quarantines exact raw pages by SHA-256, removes
duplicate repository IDs, and applies a deterministic `(-stars, repository_id)`
ordering. It then builds and validates one immutable catalog publication.

There is no cross-run Search page resume. Stars and recent pushes can reorder
pages between runs, so resuming page 7 from yesterday could omit or duplicate
members. Git history is the durable history of successful ranked snapshots.
If discovery or validation fails, the existing published catalog remains
unchanged.

## Repository publication

The repository tracks the validated human and machine indexes:

- `catalog/README.md`
- `catalog/catalog.json`
- `catalog/catalog.yaml`
- `catalog/manifest.json`
- `catalog/modules/*.md`

The root `README.md` contains one generated section between fixed markers. The
section shows the active selection policy, snapshot size, GitHub match count,
1,000-result limit, generated time, and links to each capability page. Manual
README content outside the markers is byte-preserved.

The workflow separates privileges. The discovery job has `contents: read` and
uploads only validated output as a one-day job-transfer artifact. A second,
minimal publish job has `contents: write`, revalidates the artifact manifest,
updates only `README.md` and `catalog/`, and makes a normal non-force bot push.
Raw response evidence is never downloaded by the write-capable job.

## Failure and security boundaries

- Search response bodies are bounded and parsed as untrusted data.
- Credentials are used only in request headers and are never persisted.
- Untrusted descriptions are not rendered on the repository homepage.
- Publication rejects symlinks, non-regular files, path traversal, unexpected
  files, digest mismatches, and malformed or duplicate README markers.
- A concurrent human update to `main` makes the bot push fail normally rather
  than overwriting it.
- The scheduled workflow publishes only a complete, validated snapshot.
