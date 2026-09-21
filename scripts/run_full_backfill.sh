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

while IFS=$'\t' read -r board start_page current_batch; do
  args=(sync-live --scope full_backfill --start-page "$start_page" --max-pages "$current_batch" \
    --max-articles "$max_articles" --index-batch-size "$index_batch_size" \
    --resume --board "$board")
  if [[ "${LIVE_CRAWL_INCLUDE_COMMENTS:-true}" != "true" ]]; then
    args+=(--no-comments)
  fi
  if [[ "${LIVE_CRAWL_INCLUDE_OCR:-true}" != "true" ]]; then
    args+=(--no-ocr)
  fi
  end_page=$((start_page + current_batch - 1))
  echo "Backfilling board $board pages $start_page-$end_page; indexing follows this slice"
  (
    flock 9
    conda_run maple-chat "${args[@]}"
  ) 9>"$crawl_lock_file"
done <<< "$schedule"
