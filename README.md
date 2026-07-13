# GitHub Module Catalog

把熱門、持續維護的 GitHub repository 整理成可搜尋、可組合的 capability
catalog。目標是在開發新專案前先找到已存在的模組，避免重複造輪子，再把合適的
library、CLI、service、plugin 或 template 組合成更完整的系統。

<!-- catalog-index:begin -->

## Ranked catalog index

This is the validated, repository-tracked index. It is refreshed from GitHub Search and is not an exhaustive mirror of GitHub.

- Policy: Minimum stars: **100**; pushed since **2025-07-13T00:00:00Z**; public, non-archived, non-fork repositories only.
- Coverage: **1,000 unique repositories** from **181,447 GitHub matches**; ranked result limit **1,000** across **10 page(s)**.
- Generated: **2026-07-13T06:43:41.195255Z**; order: **stars descending**, then repository ID.

[Full ranked catalog](catalog/README.md) · [JSON](catalog/catalog.json) · [YAML](catalog/catalog.yaml)

### Capability index

- [`ai-ml`](catalog/modules/ai-ml.md) — 230
- [`api-backend`](catalog/modules/api-backend.md) — 65
- [`auth`](catalog/modules/auth.md) — 7
- [`cli`](catalog/modules/cli.md) — 76
- [`database-storage`](catalog/modules/database-storage.md) — 41
- [`devops`](catalog/modules/devops.md) — 30
- [`media`](catalog/modules/media.md) — 43
- [`observability`](catalog/modules/observability.md) — 17
- [`security`](catalog/modules/security.md) — 25
- [`testing`](catalog/modules/testing.md) — 12
- [`web-ui`](catalog/modules/web-ui.md) — 42
<!-- catalog-index:end -->

## Discovery 根據什麼？

排程掃描不是索引所有 GitHub 專案。預設 query 是：

```text
stars:>=100 pushed:>=<run-start-minus-365-days> archived:false is:public
```

GitHub Search 以 stars 由高到低回傳結果；系統再逐筆確認：

- star 數至少 100；
- `pushed_at` 不早於本次執行開始時間往前 365 天的 UTC cutoff；
- repository 是 public、未 archived、不是 fork；
- 最終順序為 stars descending，同星數再依 GitHub numeric repository ID。

`min_stars`、`active_within_days`、`max_pages` 都可由 workflow dispatch
調整。GitHub Search 對單一 query 最多只提供 1,000 筆，因此 catalog 會明確標示為
「top ranked window」，不會宣稱涵蓋全部符合條件的 repository。限制與 qualifier
語意以 GitHub 官方的 [Search REST API](https://docs.github.com/en/rest/search/search)
和 [repository search qualifiers](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories)
為準。

## 專案如何分類？

Discovery 決定「哪些 repository 值得進入這次索引」；classification 才決定
「每個 repository 提供哪些 capability」。目前 deterministic classifier 只使用經過
驗證的 metadata：topics、description tokens、primary language、archived/disabled
狀態及 SPDX license。每個 assertion 都保留 taxonomy 版本、classifier 版本、信心值、
證據、來源 observation hash 與 reuse status。

Repository 不等於單一 module：一個 repository 可以提供多個 capability；同一個
capability 也可以有多個 repository 實作。Public 也不等於可直接重用；license 或
lifecycle 不明時只標為 `discovery_only`，不會標為 `safe_to_integrate`。

## Tracked index

成功的排程會把驗證後結果直接 commit 到 repository，而不是只留在 GitHub Actions
頁面底部的 Artifacts：

- `catalog/README.md` — 完整人類可讀排名；
- `catalog/catalog.json` — 主要 machine-readable index；
- `catalog/catalog.yaml` — YAML index；
- `catalog/manifest.json` — snapshot metadata 與每個檔案的 SHA-256；
- `catalog/modules/*.md` — capability 對應的 repository 清單。

Actions artifact 只在 read-only discovery job 與 write-capable publish job 之間暫時
傳遞已驗證輸出，保留一天。Raw GitHub response、SQLite state 與 token 不會提交到 Git，
也不會交給 write-capable job。

## Quick start

```bash
uv sync --all-groups --locked
uv run ghmod init --workspace .local/catalog
export GITHUB_TOKEN="$(gh auth token)"
uv run ghmod refresh \
  --workspace .local/catalog \
  --min-stars 100 \
  --active-within-days 365 \
  --max-pages 10
uv run ghmod validate-output --workspace .local/catalog
python3 scripts/publish_catalog.py \
  --source .local/catalog/catalog-output \
  --repository .
```

Token 只透過 request header 傳給 GitHub，不會寫入 catalog、raw store、log 或 command
summary。不要輸出 token，也不要把它放進 URL 或 committed `.env`。

## Architecture and safety

- Ranked discovery 每次都從 Search page 1 建立完整 snapshot，不跨次續抓動態排名頁。
- 每頁 response 有大小限制、schema validation、`incomplete_results` rejection 與
  SHA-256 raw evidence；snapshot 不完整就保留上一次 published catalog。
- Crawler 不 clone repository，也不執行第三方 code、workflow、build 或 instruction。
- Publisher 會重驗 manifest、digest、精確檔案集合、selection、rank、coverage、
  symlink/path traversal 與 README marker，才更新 tracked index。
- Discovery job 只有 `contents: read`；publish job 只有 `contents: write`，只 stage
  `README.md` 與 `catalog/`，並使用一般 non-force push。

原本基於 `GET /repositories?since=<id>` 的 broad repository-feed cursor 仍保留為可選的
exhaustive-feed primitive，但不再是首頁排程 catalog 的 discovery policy。詳細操作請看
[operations guide](docs/operations.md)，分類與授權語意請看
[taxonomy guide](docs/taxonomy.md)。

## License

Catalog software 採 MIT License。第三方 repository 的 metadata、名稱、程式碼及其他
內容仍保留原權利與授權條款。
