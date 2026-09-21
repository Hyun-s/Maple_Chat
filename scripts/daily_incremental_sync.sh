#!/usr/bin/env bash

# Daily ID-deduplicated community sync: all recent posts first, then one backfill page.

set +x
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

readonly -a BOARDS=(2294 2295 2296 2297 2298 2300 2304)
latest_max_pages="${DAILY_LATEST_MAX_SCAN_PAGES:-20}"
target_page="${FULL_BACKFILL_TARGET_PAGE:-200}"
retry_count="${DAILY_CRAWL_RETRIES:-5}"
retry_delay="${DAILY_CRAWL_RETRY_DELAY:-30}"
crawl_lock_file="${LIVE_CRAWL_LOCK_FILE:-/tmp/maple-chat-live-crawl.lock}"

require_uint() {
  local name="$1" value="$2" maximum="$3"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > maximum )); then
    echo "$name must be an integer between 1 and $maximum" >&2
    exit 2
  fi
}

require_uint DAILY_LATEST_MAX_SCAN_PAGES "$latest_max_pages" 100
require_uint FULL_BACKFILL_TARGET_PAGE "$target_page" 10000
require_uint DAILY_CRAWL_RETRIES "$retry_count" 20
require_uint DAILY_CRAWL_RETRY_DELAY "$retry_delay" 3600

start_database

# Pages beyond the approved sample and multi-board operation require full-backfill approval.
conda_run python - <<'PY'
from maple_chat.config import ProcessRole, load_settings
from maple_chat.crawler.policy import CrawlScope, require_live_crawl

settings = load_settings(ProcessRole.CRAWLER_WORKER)
require_live_crawl(
    settings,
    "https://www.inven.co.kr/board/maple/2304?p=2",
    CrawlScope.FULL_BACKFILL,
)
print("Daily incremental full-backfill approval gate passed")
PY

exec 8>"$crawl_lock_file"
flock 8

run_with_retry() {
  local label="$1"
  shift
  local attempt rc delay
  for attempt in $(seq 1 "$retry_count"); do
    if "$@"; then
      return 0
    else
      rc=$?
    fi
    if (( attempt == retry_count )); then
      echo "$label failed after $attempt attempts (last exit $rc)" >&2
      return "$rc"
    fi
    delay=$((retry_delay * attempt))
    echo "$label failed transiently; retrying in ${delay}s ($attempt/$retry_count)" >&2
    sleep "$delay"
  done
}

crawl() {
  conda_run maple-chat crawl-live \
    --scope full_backfill \
    --max-articles 100 \
    --no-ocr \
    "$@"
}

# A latest-page call is intentionally one page/transaction. A retry can therefore
# never commit page 1 and then incorrectly stop before a failed page 2.
last_crawl_json=""
crawl_and_capture() {
  local output
  if output="$(crawl "$@")"; then
    printf '%s\n' "$output"
  else
    local rc=$?
    [[ -n "$output" ]] && printf '%s\n' "$output"
    return "$rc"
  fi
  last_crawl_json="$(printf '%s\n' "$output" | python3 -c '
import json, sys
for line in reversed(sys.stdin.read().splitlines()):
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(value, dict) and "board_id" in value:
        print(json.dumps(value))
        break
else:
    raise SystemExit("crawl command did not emit a result object")
')"
}

json_int() {
  local key="$1"
  python3 -c 'import json,sys; print(int(json.load(sys.stdin)[sys.argv[1]]))' \
    "$key" <<<"$last_crawl_json"
}

checkpoint() {
  local board_id="$1" row
  row="$(compose_local exec -T postgres psql -X -qAt -F '|' \
    -U maple_chat -d maple_chat -c \
    "SELECT COALESCE(crawl_checkpoint->>'next_backfill_page', '1'),
            COALESCE(crawl_checkpoint->>'backfill_complete', 'false')
       FROM boards WHERE board_id = $board_id")"
  [[ -n "$row" ]] && printf '%s\n' "$row" || printf '1|false\n'
}

failures=0
declare -A latest_succeeded=()

# Phase 1 has strict priority: finish recent-ID discovery for every board before
# any historical checkpoint is advanced. Continue until a fully-known page marks
# the boundary between new arrivals and data already stored, with a safety bound.
for board in "${BOARDS[@]}"; do
  latest_succeeded[$board]=1
  echo "[$(date --iso-8601=seconds)] Board $board: collecting all newly listed articles"
  for page in $(seq 1 "$latest_max_pages"); do
    if ! run_with_retry "board $board latest page $page" \
      crawl_and_capture --board "$board" --start-page "$page" --max-pages 1 \
        --preserve-backfill-checkpoint --skip-existing; then
      failures=1
      latest_succeeded[$board]=0
      break
    fi
    if (( $(json_int unseen_articles) == 0 )); then
      echo "[$(date --iso-8601=seconds)] Board $board: known-ID boundary reached at page $page"
      break
    fi
    if (( page == latest_max_pages )); then
      echo "Board $board reached DAILY_LATEST_MAX_SCAN_PAGES=$latest_max_pages before a known-ID boundary" >&2
      failures=1
      latest_succeeded[$board]=0
    fi
  done
done

# Phase 2 is lower priority and strictly bounded to one historical listing page
# per board. If recent discovery failed, skip that board's backfill for this run.
for board in "${BOARDS[@]}"; do
  if [[ "${latest_succeeded[$board]}" != "1" ]]; then
    echo "[$(date --iso-8601=seconds)] Board $board: skipping backfill because latest sync failed" >&2
    continue
  fi
  IFS='|' read -r next_page complete < <(checkpoint "$board")
  if [[ "$complete" == "true" ]] || (( next_page > target_page )); then
    continue
  fi
  echo "[$(date --iso-8601=seconds)] Board $board: continuing one backfill page at $next_page"
  if ! run_with_retry "board $board backfill page $next_page" \
    crawl --board "$board" --start-page "$next_page" --max-pages 1 \
      --resume --skip-existing; then
    failures=1
  fi
done

conda_run maple-chat sync-knowledge
"$ROOT_DIR/scripts/index_with_gpu_handoff.sh"
mkdir -p "$ROOT_DIR/logs"
conda_run maple-chat audit-knowledge >>"$ROOT_DIR/logs/knowledge-audit.jsonl"

if (( failures != 0 )); then
  echo "Daily incremental sync completed with at least one crawl failure; committed data was indexed" >&2
  exit 5
fi
echo "Daily incremental sync completed"
