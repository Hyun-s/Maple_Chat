# Operations Runbook

## Safety state

- Live crawling remains disabled until the external approval file authorizes the exact scope.
- Discord remains a single private guild deployment. Code completion does not open the release gate.
- PostgreSQL and application services expose no inbound host ports in the production Compose file.

## Local private-guild startup

The supported local path uses the `maple-chat` Conda environment. Only PostgreSQL and the local
vLLM run in containers.

1. Copy `.env.example` to `.env`, set mode `0600`, and fill the Discord IDs/secrets.
2. Store the crawl approval JSON outside the repository and set its absolute path.
3. Run `scripts/setup_local.sh` once, then `scripts/run_live_bot.sh`.

Startup validates role configuration before expensive work, starts loopback PostgreSQL on port
`55432`, migrates it, runs one bounded live sync, verifies or starts the local Qwen vLLM, and then
starts the Discord gateway client. It never prints the token, database password, or PII salt.
While the bot is running, `daily_sync_loop.sh` starts an ID-deduplicated incremental run at 05:00
Asia/Seoul unless `RUN_DAILY_SYNC=false`. Page 1 is refreshed for edits and comments; additional
recent pages fetch details only for unseen `(board_id, remote_article_id)` values. Each run also
continues three historical pages per board and overlaps the two pages before each checkpoint to
recover IDs shifted across page boundaries by newly inserted posts. A shared crawl lock prevents
overlapping Inven jobs.

`weekly_patch_notes_loop.sh` independently refreshes the rolling 365-day official Nexon update
window every Thursday at 05:00 Asia/Seoul unless `RUN_WEEKLY_PATCH_SYNC=false`. It scans update
listing pages only to the cutoff and fetches detail pages only for new or modified entries. Its
separate lock prevents overlapping official syncs without weakening the Inven approval boundary.

For an independent sync:

```bash
scripts/sync_live.sh
```

For an immediate official patch-note incremental sync and index:

```bash
scripts/sync_patch_notes.sh
```

The official client is GET-only and accepts only HTTPS `maplestory.nexon.com/News/Update` URLs.
`PATCH_NOTES_RETENTION_DAYS=365`, `PATCH_NOTES_WEEKLY_AT=05:00`, and
`PATCH_NOTES_MAX_LIST_PAGES=60` are the default safety bounds. Official patch-note chunks carry
`source_authority=official`; this is a factual provenance signal, never permission to execute text
found in a page.

`approved_sample` is hard-limited to one board, page 1, and five articles. A larger board/page set
requires approval evidence containing `full_backfill`. Every live request revalidates the target,
scope, and external evidence; parser drift, CAPTCHA, and repeated 403/429 responses stop the run.
Set `LIVE_CRAWL_RESUME=true` for full backfill so each invocation resumes from the persisted
per-board page checkpoint.

For an approved full-history run across all seven boards, use:

```bash
scripts/run_full_backfill.sh
```

The command validates the external `full_backfill` evidence before database startup, then crawls
from each board's checkpoint with comments enabled and OCR disabled by default. The runner rotates
through all seven boards in `FULL_BACKFILL_PAGE_BATCH`-page slices (10 by default) and stops each
board at `FULL_BACKFILL_TARGET_PAGE` (200 by default). The final slice is shortened so it never
crosses the target. `FULL_BACKFILL_MAX_ARTICLES_PER_PAGE` bounds each listing page; rerunning is
safe and resumes from committed checkpoints. Crawling and embedding do not call the generative LLM. The Discord bot
uses the Qwen3.8-27B `local-coder` service from
`/home/hyuns/local-claude-code/docker-compose.qwen38-27b.yml` on loopback port 8001.

## Health and metrics

Report bot, database, embedding/reranker, vLLM, and scheduler freshness separately. Aggregate
readiness is true only when every configured probe is healthy. JSON logs use event codes and
hashed identifiers; never attach questions, source bodies, OCR text, nicknames, authentication
tokens, salts, or database URLs as log context. Numeric model token counts are telemetry and may be
logged because they contain no token text or credential value.

Every completed Discord answer emits `discord_answer_completed` with `answer_id`, answer mode,
RAG flag, input/output/total token counts, input/output effective TPS, model-call time, end-to-end
QA time, and model-call count. The same user-facing metrics appear at the bottom of model-generated
answers. Input TPS is `prompt_tokens / TTFT`, so it is an **effective prefill rate including local
queue and transport overhead**, not a hardware-only kernel benchmark. Output TPS is
`completion_tokens / (stream completion time - first content time)`. A canonical-name repair that
calls the model twice is aggregated rather than hiding the first call. Retrieval-only or
insufficient-evidence paths log zero model calls and do not add a misleading token footer.

Required counters/timers include Discord result and queue latency, retrieval/rerank/LLM latency,
insufficient-evidence and fallback counts, per-board HTTP/parser results, queue depth, backfill
progress, and embedding throughput.

## Backup

Run daily and weekly jobs from the repository root while Compose is healthy:

```bash
BACKUP_DIR=/secure/maple-chat/backups scripts/backup_postgres.sh daily
BACKUP_DIR=/secure/maple-chat/backups scripts/backup_postgres.sh weekly
```

Backups are data-only custom-format archives. Daily files older than 7 days and weekly files older
than 28 days are deleted. Vector data is excluded by default because it is revisioned and
reproducible; set `BACKUP_INCLUDE_VECTORS=true` only when the storage/recovery tradeoff is accepted.

## Restore rehearsal

The restore command only accepts a new isolated `maple_chat_restore_*` database. It refuses an
ordinary or production-like database name.

```bash
scripts/restore_postgres.sh /secure/maple-chat/backups/daily-...dump \
  maple_chat_restore_drill_20260804
```

The command creates the empty database, runs all migrations, restores data transactionally,
clears stale leases, and verifies the single-active embedding revision invariant. Validate row
counts and a fixture retrieval before dropping the drill database.

## Production corpus reconstruction after the 2026-09-03 reset

The one-shot recovery entrypoint reconstructs pages 1 through 200 for every approved community
board: Warrior, Mage, Archer, Thief, Pirate, Q&A, and Tips. It also synchronizes the bundled
Knowledge Graph, refreshes the rolling 365-day official patch-note window, builds structure-aware
chunks and BGE-M3 embeddings, verifies the completed state, and creates pre/post recovery backups
with vectors included.

Inspect the plan without touching the database or network:

```bash
scripts/recover_deleted_production_data.sh --plan
```

Run it unattended in tmux from any shell:

```bash
tmux new-session -d -s maple-chat-recovery \
  "bash -lc 'cd /home/hyuns/project/Maple_Chat && exec scripts/recover_deleted_production_data.sh'"
```

Follow the master log and the three parallel collection lanes:

```bash
tail -f logs/recovery/latest/recovery.log
tail -f logs/recovery/latest/community.log
tail -f logs/recovery/latest/patch-notes.log
tail -f logs/recovery/latest/knowledge.log
```

The Knowledge Graph, Nexon collector, and Inven collector run concurrently. Community board
requests are deliberately serialized through one respectful client and shared lock: starting seven
separate crawlers would bypass the per-client one-request-per-second pacing policy. Expensive model
loading is deferred until all collection finishes, so BGE-M3 is loaded only once. On the CUDA path,
the external `gpu-job` wrapper drains and temporarily stops the exact active vLLM container, runs
the embedding build exclusively, and restores vLLM afterward. Set
`RECOVERY_EMBEDDING_DEVICE=cpu` only when a much slower CPU-only embedding rebuild is preferred.

When DCM's pinned BGE-M3 TEI service is already running, set `EMBEDDING_PROVIDER=remote`. Host-run
commands use `EMBEDDING_BASE_URL=http://127.0.0.1:8081/v1`; Compose app services join the external
`dcm-model-plane` network and default to `http://dcm-embedding:80/v1`. Keep
`EMBEDDING_REMOTE_MODEL=bge-m3` as the OpenAI-compatible request alias. Both incremental indexing
and production recovery then leave the managed model stack running and use `POST /v1/embeddings`.
The client fails closed on
HTTP errors, `/info` model ID or SHA mismatch, response model mismatch, wrong count/order,
non-finite values, non-1024 dimensions, or a vector
whose L2 norm is not approximately one. It never silently falls back and allocates a local GPU
model. To use the existing offline SentenceTransformer path intentionally, set
`EMBEDDING_PROVIDER=local`; only that local CUDA path invokes `gpu-job`.

Before each batch, Maple Chat requires TEI `/info` to report model ID `BAAI/bge-m3` and model SHA
`5617a9f61b028005a4858fdac845db406aefb181`; no embedding request is sent if either differs. Keep
that revision pinned in Maple Chat and the DCM service configuration together. The exact allowed
container DNS name is `dcm-embedding`; do not assume a host port bound to loopback is reachable
through `host.docker.internal`.

The script is idempotent and uses the committed per-board checkpoints on rerun. A transient crawl
failure is retried five consecutive times per page by default, with an increasing delay. Progress
committed before a later request fails resets the retry counter, and each retry resumes within the
same page slice instead of recrawling already committed pages. It restores source coverage, not a
byte-identical snapshot: community posts edited or deleted after the original crawl and the old
answer/provenance history cannot be recreated from the remaining progress logs.

## Graceful shutdown

Stop acquiring new leases, allow in-flight DB transactions to commit or roll back within the
configured grace period, cancel remaining tasks, then stop the process. Expired leases are
reclaimed on startup. Never acknowledge a job before its transaction commits.
