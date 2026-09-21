#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

mode="${1:-configured}"
if (( $# > 1 )) || [[ "$mode" != "configured" && "$mode" != "--daily-latest" ]]; then
  echo "usage: scripts/sync_live.sh [--daily-latest]" >&2
  exit 2
fi

if [[ "$mode" == "--daily-latest" ]]; then
  exec "$ROOT_DIR/scripts/daily_incremental_sync.sh"
fi
start_database
scope="${LIVE_CRAWL_SCOPE:-approved_sample}"
start_page="${LIVE_CRAWL_START_PAGE:-1}"
max_pages="${LIVE_CRAWL_MAX_PAGES:-1}"
max_articles="${LIVE_CRAWL_MAX_ARTICLES:-5}"
boards_value="${LIVE_CRAWL_BOARD_IDS:-2304}"
resume="${LIVE_CRAWL_RESUME:-false}"
preserve_backfill_checkpoint="false"
read -r -a boards <<< "${boards_value//,/ }"

args=(sync-live --scope "$scope" --start-page "$start_page" --max-pages "$max_pages" \
  --max-articles "$max_articles" --index-batch-size "${INDEX_BATCH_SIZE:-32}")
for board in "${boards[@]}"; do
  args+=(--board "$board")
done
if [[ "${LIVE_CRAWL_INCLUDE_COMMENTS:-true}" != "true" ]]; then
  args+=(--no-comments)
fi
if [[ "${LIVE_CRAWL_INCLUDE_OCR:-true}" != "true" ]]; then
  args+=(--no-ocr)
fi
if [[ "$resume" == "true" ]]; then
  args+=(--resume)
fi
if [[ "$preserve_backfill_checkpoint" == "true" ]]; then
  args+=(--preserve-backfill-checkpoint)
fi

cd "$ROOT_DIR"
conda_run maple-chat "${args[@]}"

# Community terminology never updates canonical knowledge automatically. The
# append-only report only surfaces explicit expansion differences for review.
mkdir -p "$ROOT_DIR/logs"
if ! conda_run maple-chat sync-knowledge >/dev/null; then
  echo "Knowledge catalog sync failed; terminology audit was skipped" >&2
elif ! conda_run maple-chat audit-knowledge >> "$ROOT_DIR/logs/knowledge-audit.jsonl"; then
  echo "Knowledge terminology audit failed; collected and indexed data remains valid" >&2
fi
