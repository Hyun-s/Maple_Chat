#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

cd "$ROOT_DIR"

# Validate the external approval artifact and its full_backfill scope before starting
# PostgreSQL or making any network request. The approval content is never printed.
conda_run python - <<'PY'
from maple_chat.config import ProcessRole, load_settings
from maple_chat.crawler.policy import CrawlScope, require_live_crawl

settings = load_settings(ProcessRole.CRAWLER_WORKER)
require_live_crawl(
    settings,
    "https://www.inven.co.kr/board/maple/2304?p=1",
    CrawlScope.FULL_BACKFILL,
)
print("Full-backfill approval gate passed")
PY

start_database

target_page="${FULL_BACKFILL_TARGET_PAGE:-200}"
max_articles="${FULL_BACKFILL_MAX_ARTICLES_PER_PAGE:-100}"
page_batch="${FULL_BACKFILL_PAGE_BATCH:-10}"
index_batch_size="${INDEX_BATCH_SIZE:-32}"
# sync-live indexes every pending row, so while a dedicated indexing worker is
# draining the backlog the crawl plane can hand the encoder over to it entirely.
inline_index="${FULL_BACKFILL_INLINE_INDEX:-true}"
approved_boards=(2294 2295 2296 2297 2298 2300 2304)
approved_board_csv="$(IFS=,; echo "${approved_boards[*]}")"
crawl_lock_file="${LIVE_CRAWL_LOCK_FILE:-/tmp/maple-chat-live-crawl.lock}"

if ! [[ "$target_page" =~ ^[0-9]+$ && "$page_batch" =~ ^[0-9]+$ ]] \
  || (( target_page < 1 || target_page > 10000 || page_batch < 1 || page_batch > target_page )); then
  echo "FULL_BACKFILL_TARGET_PAGE must be 1..10000 and PAGE_BATCH must not exceed it" >&2
  exit 2
fi

echo "Planning resumable round-robin backfill in $page_batch-page slices through page $target_page"
schedule="$(conda_run python - "$target_page" "$page_batch" "$approved_board_csv" <<'PY'
import asyncio
import sys

import sqlalchemy as sa

from maple_chat.config import ProcessRole, load_settings
from maple_chat.db.models import Board
from maple_chat.db.session import create_engine, create_session_factory
from maple_chat.scheduler.planner import BOARD_NAMES, plan_round_robin_backfill


async def main() -> None:
    target_page = int(sys.argv[1])
    page_batch = int(sys.argv[2])
    approved_boards = tuple(int(value) for value in sys.argv[3].split(","))
    if approved_boards != tuple(BOARD_NAMES):
        raise RuntimeError("shell and application board scopes differ")
    engine = create_engine(load_settings(ProcessRole.CRAWLER_WORKER))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    sa.select(Board.board_id, Board.crawl_checkpoint).where(
                        Board.board_id.in_(approved_boards)
                    )
                )
            ).all()
        checkpoints: dict[int, int | None] = {}
        for board_id, checkpoint in rows:
            if checkpoint.get("backfill_complete") is True:
                checkpoints[board_id] = None
            else:
                checkpoints[board_id] = int(checkpoint.get("next_backfill_page", 1))
        for item in plan_round_robin_backfill(
            checkpoints,
            target_page=target_page,
            page_batch=page_batch,
        ):
            print(f"{item.board_id}\t{item.start_page}\t{item.max_pages}")
    finally:
        await engine.dispose()


asyncio.run(main())
PY
)"

if [[ -z "$schedule" ]]; then
  echo "All board checkpoints are complete or already past page $target_page"
  exit 0
fi

slice_retries="${FULL_BACKFILL_SLICE_RETRIES:-3}"
model_plane_attempts="${FULL_BACKFILL_MODEL_PLANE_ATTEMPTS:-5}"
model_plane_wait_seconds="${FULL_BACKFILL_MODEL_PLANE_WAIT_SECONDS:-120}"
failed_slices=0

# A queued 05:00 discovery run must not lose the crawl plane to the next backfill
# slice. flock alone lets the faster re-acquirer win indefinitely, so the daily
# script raises a flag naming its pid and this loop declines to start another slice.
crawl_priority_file="${LIVE_CRAWL_PRIORITY_FILE:-/tmp/maple-chat-live-crawl.priority}"
priority_max_wait="${FULL_BACKFILL_PRIORITY_MAX_WAIT:-900}"
priority_waited=0

wait_for_crawl_priority() {
  local owner
  priority_waited=0
  while [[ -e "$crawl_priority_file" ]]; do
    owner="$(cat "$crawl_priority_file" 2>/dev/null || true)"
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
      echo "Removing stale crawl priority flag left by pid $owner" >&2
      rm -f "$crawl_priority_file"
      return 0
    fi
    if (( priority_waited >= priority_max_wait )); then
      echo "Crawl priority request exceeded ${priority_max_wait}s; continuing the backfill" >&2
      return 0
    fi
    echo "Daily sync holds a crawl request; deferring this slice for 15s" >&2
    sleep 15
    priority_waited=$((priority_waited + 15))
  done
}

# Indexing follows each crawled slice, so wait for a degraded encoder to be
# recovered instead of spending the remaining crawl budget on doomed requests.
model_plane_ready() {
  local attempt
  for attempt in $(seq 1 "$model_plane_attempts"); do
    if "$ROOT_DIR/scripts/verify_model_plane.sh"; then
      return 0
    fi
    echo "Model plane unavailable (attempt $attempt/$model_plane_attempts); waiting ${model_plane_wait_seconds}s" >&2
    sleep "$model_plane_wait_seconds"
  done
  return 1
}

while IFS=$'\t' read -r board start_page current_batch; do
  args=(sync-live --scope full_backfill --start-page "$start_page" --max-pages "$current_batch" \
    --max-articles "$max_articles" --index-batch-size "$index_batch_size" \
    --resume --board "$board")
  if [[ "$inline_index" != "true" ]]; then
    args+=(--no-index)
  fi
  if [[ "${LIVE_CRAWL_INCLUDE_COMMENTS:-true}" != "true" ]]; then
    args+=(--no-comments)
  fi
  if [[ "${LIVE_CRAWL_INCLUDE_OCR:-true}" != "true" ]]; then
    args+=(--no-ocr)
  fi
  end_page=$((start_page + current_batch - 1))
  echo "Backfilling board $board pages $start_page-$end_page (inline indexing: $inline_index)"
  if ! model_plane_ready; then
    echo "Aborting backfill: embedding model plane unrecoverable; board checkpoints stay resumable" >&2
    exit 1
  fi
  attempt=1
  while true; do
    wait_for_crawl_priority
    # A transient crawler or encoder timeout must not abandon the remaining
    # schedule: board checkpoints make each slice cheap to retry.
    if (
      flock 9
      conda_run maple-chat "${args[@]}"
    ) 9>"$crawl_lock_file"; then
      break
    fi
    if (( attempt >= slice_retries )); then
      echo "Slice board=$board pages=$start_page-$end_page failed after $attempt attempts; continuing" >&2
      failed_slices=$((failed_slices + 1))
      break
    fi
    echo "Slice board=$board attempt $attempt/$slice_retries failed; retrying in $((attempt * 30))s" >&2
    sleep $((attempt * 30))
    attempt=$((attempt + 1))
  done
done <<< "$schedule"

if (( failed_slices > 0 )); then
  echo "Backfill finished with $failed_slices failed slice(s); re-run to resume from checkpoints" >&2
  exit 1
fi
echo "Backfill schedule complete through page $target_page"
