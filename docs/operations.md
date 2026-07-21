# Operations guide

首頁 catalog 是「熱門且近期仍維護的 ranked snapshot」，不是 GitHub 全量鏡像。每次
執行會重新從 Search page 1 建立一份完整、可驗證的 top-ranked window；任何步驟失敗
都不會取代上一版 tracked catalog。

## Ranked discovery policy

預設 policy：

```text
stars:>=100 pushed:>=<run-start-minus-365-days> archived:false is:public
```

Request 使用 `sort=stars`、`order=desc`、`per_page=100`，最多 10 pages。UTC cutoff
在 command 開始時固定，避免長時間執行期間漂移。每筆 response item 還會在本地重驗
star 數、`pushed_at`、public visibility、archived 與 fork 狀態，再依
`(-stargazers_count, repository_id)` 排出 deterministic rank。

GitHub Search 對一個 query 最多只提供 1,000 results，因此 `api_total_count` 可能遠大於
實際 catalog size。首頁與 manifest 會同時紀錄 API matches、ranked result limit、實際
entries、pages、query 與 raw page hashes；它們只表示這次 top window，不代表所有 GitHub
repository。參考 GitHub 官方 [Search REST API](https://docs.github.com/en/rest/search/search)。

## Credentials

本機使用已認證的 GitHub CLI 取得可撤銷 credential，不要顯示 token：

```bash
export GITHUB_TOKEN="$(gh auth token)"
```

`refresh` 只把 token 放在 Authorization header。Token 不會寫入 raw objects、catalog、
manifest、SQLite、logs 或 command summaries。不要使用 `echo`、shell tracing、URL token、
committed `.env`，也不要把 credential 貼到聊天中。

GitHub Actions 使用每個 job 自動建立、repo-scoped 的 `GITHUB_TOKEN`。Discovery job 只有
`contents: read`；publish job 只有 `contents: write`。兩個 job 不共享 credential。

## Local ranked runbook

```bash
uv sync --all-groups --locked
uv run ghmod init --workspace .local/catalog
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

`refresh` 會先在 candidate directory 建立 JSON、YAML、完整 catalog README、
`taxonomy.md`、有命中資料的 module pages 與 `manifest.json`，再以 raw Search evidence
驗證。JSON/YAML 使用 schema `1.1.0`，內含 Taxonomy v2 的完整 capability definition graph；
YAML artifact 採 canonical pretty JSON（JSON 是 YAML 1.2 的合法子集合），讓無第三方
dependency 的 write-capable publisher 也能逐 byte 重建。`taxonomy.md` 則把 parent/child
關係與目前 counts 直接呈現在 GitHub。只有完整 snapshot 才會 atomically 取代
`<workspace>/catalog-output`。`validate-output` 可單獨重做 raw-backed validation。

### Whole-snapshot retry policy

GitHub Search 不提供跨分頁的 snapshot isolation，因此 `total_count`、page membership 或
`incomplete_results` 可能在一次十頁擷取途中短暫漂移。`refresh` 遇到
`GitHubSearchError` 時最多執行三次完整嘗試，分別在第二、第三次之前等待 5 秒與 15 秒。
每次嘗試都關閉舊 source、重新建立 client 並從 page 1 開始；selection cutoff 與 page
budget 在整個 command 期間保持不變。成功摘要中的 `discovery_attempts` 會顯示實際嘗試
次數，連續失敗則輸出最後一個安全的 Search 錯誤原因。

重試只包住 Search snapshot boundary。Raw-backed validation、分類、artifact rendering 與
publication 任一步驟失敗仍立即中止，而且舊的完整 catalog 不會被取代。曾評估通用的
Tenacity 與 request-level 的 HTTPX/httpx-retries：後者無法恢復跨頁一致性；前者能做到，
但這裡只有固定三次的同步 orchestration policy，新增 runtime dependency 的維護與供應鏈
成本高於一個明確、可測的 bounded loop。若未來需要依 response headers、jitter 或多種
backoff policy，再重新評估 Tenacity。

Publisher 是第二個 trust boundary。它不需要第三方 dependency，會重驗：

1. source tree 只有 manifest 宣告的 regular files，沒有 symlink、traversal 或額外檔案；
2. 每個 artifact SHA-256 與 manifest 一致；
3. JSON metadata 與 manifest 一致；
4. selection、target count、pages、query、raw hashes、rank、stars、push cutoff、public、
   archived、fork 與 repository ID order 一致；
5. capability definitions ID 唯一、排序固定、parent 存在、graph 無 cycle，且每個 leaf
   assertion 都包含完整 ancestors；publisher 另限制 definitions、parents、edges、depth 與
   render expansion，避免惡意 DAG 放大資源消耗；
6. catalog README、`taxonomy.md` 與所有 module pages 必須和 publisher 從已驗證 JSON
   重建的 canonical Markdown 逐 byte 相同；只重算 manifest digest 無法注入內容；
7. taxonomy 與 module page file set 完整，root README 只有一對合法 managed markers。

成功後 `catalog/` 是完整 replacement，舊 module pages 不會殘留；README markers 外的
manual bytes 保持不變。

## Scheduled publication

`.github/workflows/discover.yml` 每六小時執行，也支援 manual dispatch 的三個 input：

- `min_stars`，預設 100；
- `active_within_days`，預設 365；
- `max_pages`，預設 10、上限 10。

資料流分成兩個 job：

1. `discover` 以 read-only checkout 執行 `refresh` 與 `validate-output`；
2. 它只上傳 `catalog-output`，作為保留一天的 job-transfer artifact；
3. `publish` checkout 同一個 `github.sha`，下載指定 artifact，重新執行 safe publisher；
4. 它只 stage `README.md` 與 `catalog/`，建立一般 commit，再 non-force push 到 default
   branch。

Raw Search bytes、workspace state 與 SQLite 不會上傳給 write-capable job，也不會 commit
到 Git。若 default branch 在執行期間前進，non-force push 會安全失敗，不會覆蓋人類
變更。若內容沒有差異，job 不建立空 commit。

Third-party Actions 全部 pin 到完整 immutable commit SHA。目前 workflow 使用
`actions/checkout` v7、`astral-sh/setup-uv` v8.3.2、`actions/upload-artifact` v7 及
`actions/download-artifact` v8；uv binary 另外固定為 `0.10.2`。

## Failure and recovery

- Search 回傳 `incomplete_results=true`、total count 漂移、short page、conflicting duplicate
  或無法填滿 unique target window：整次 refresh 失敗。
- Candidate validation 或 publisher validation 失敗：舊 catalog 不變。
- README 安裝使用 atomic `os.replace`，正式 README path 不會短暫消失。
- Catalog directory 在 portable standard library 中無法與非空舊目錄做單一原子交換；
  publisher 會先建立/fsync recovery backup，再安裝新版。
- Rollback 的 README、catalog、fsync 彼此獨立嘗試。若 recovery 本身也失敗，唯一舊版
  backup 會以 `.README.md.backup-*` 或 `.catalog-backup-*` 保留，絕不由 finally 刪除。
- Publisher 非零退出時 workflow 不會 commit 或 push，因此 GitHub 遠端仍維持上一個
  完整 commit。

保留失敗 workspace 供診斷；不要手動刪 recovery backup，直到確認 tracked README 與
catalog 都已恢復。重新執行前先處理或移走殘留 recovery path。

## Classification, license, and execution safety

Selection 只決定 snapshot membership，不代表 repository 可安全整合。`rules-v2`
classifier 根據 validated topics、description token/phrase、resource exclusions 與
leaf-specific exclusions 產生 capability assertions，並自動補齊 parent ancestors。目前不
使用 AI/LLM。未知、缺失、non-permissive license 或不明 lifecycle 仍為
`discovery_only`。`safe_to_integrate` 只是保守的技術 policy signal，不是法律意見。

Crawler 永遠不 clone 或執行第三方 repository 的 code、workflow、package scripts、build
files 或 instruction。Untrusted descriptions 不會出現在 root homepage managed section。

## Optional broad-feed commands

原 MVP 的 `discover`、`status`、`classify`、`build`、`validate` 與
`GET /repositories?since=<repository-id>` cursor 仍保留，供 broad inventory 或研究用途。
它有 durable SQLite/raw-page recovery semantics，但不再驅動 scheduled homepage catalog，
也不能用來解釋目前 ranked index 的 selection basis。

## GitHub App migration

規模擴大後可把 token factory 換成 GitHub App installation-token provider。維持 read-only
metadata permission、短效 token、header-only credential、host allowlist、response byte
limit、固定 page budget、raw evidence validation 與 job privilege separation；不要把 App
private key 放進 catalog workspace。
