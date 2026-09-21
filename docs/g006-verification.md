# G006 Verification Evidence

- Exactly seven approved boards are seeded with dynamically discovered categories.
- KST daily and recent-first planners write durable runs/jobs and suppress overlapping active
  runs with advisory and partial-unique guards.
- Per-board JSON checkpoints preserve next page, earliest article, recent readiness, completion,
  and last success for crash resume.
- Fixture ingestion of all seven boards is duplicate-free on a second pass, including comment-only
  outbox changes; board failures can finish a run as `partial` without blocking other boards.
- Full live backfill calls the independent `full_backfill` approval scope and fails closed while
  `LIVE_CRAWL_ENABLED=false`.
