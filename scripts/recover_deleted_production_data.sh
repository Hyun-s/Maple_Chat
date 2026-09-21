#!/usr/bin/env bash

# Rebuild the production corpus after the 2026-09-03 accidental database reset.
#
# The recovery is coverage-equivalent, not byte-identical: live community posts may
# have changed or disappeared since the original crawl.  The bundled knowledge graph
# is deterministic and is verified against its exact catalog counts.

set +x
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

declare -ar BOARD_IDS=(2294 2295 2296 2297 2298 2300 2304)
declare -ar BOARD_NAMES=("전사" "마법사" "궁수" "도적" "해적" "질문과 답변" "팁과 노하우")
declare -ar TARGET_PAGES=(200 200 200 200 200 200 200)

usage() {
  cat <<'EOF'
Usage:
  scripts/recover_deleted_production_data.sh [--plan]

Environment overrides:
  RECOVERY_PAGE_BATCH=10          Inven pages committed per resumable slice
  RECOVERY_CRAWL_RETRIES=5        Retries for a transient failed crawl slice
  RECOVERY_CRAWL_RETRY_DELAY=30   Base retry delay in seconds
  RECOVERY_INDEX_BATCH_SIZE=32    BGE-M3 embedding batch size
  EMBEDDING_PROVIDER=local        local or remote HTTP embedding provider
  RECOVERY_EMBEDDING_DEVICE=cuda  cuda (fast) or cpu (does not stop vLLM)
  RECOVERY_PATCH_DAYS=365         Official patch-note window
  RECOVERY_PATCH_MAX_PAGES=60     Maximum official listing pages
  RECOVERY_BACKUP=true            Back up before and after recovery

The local CUDA path uses the model manager's gpu-job wrapper. It drains and stops
the exact running vLLM container for indexing, then restores that container. The
remote provider leaves the managed model stack running and fails closed if unavailable.
EOF
}

print_plan() {
  cat <<'EOF'
Recovery stages
  1. Validate the external full_backfill approval and database configuration.
  2. Back up the current partial database, including vectors.
  3. Remove only the known integration-test answer/chunk fixture.
  4. Run these independent jobs concurrently:
       - bundled Knowledge Graph synchronization
       - official Nexon patch-note synchronization without indexing
       - respectful Inven community crawl to the historical page coverage
  5. Run one bulk BGE-M3 chunk/embedding build after every source is collected.
  6. Audit terminology and verify graph counts, checkpoints, statuses, and vectors.
  7. Back up the recovered database, including vectors.

Historical community coverage targets
EOF
  local i
  for i in "${!BOARD_IDS[@]}"; do
    printf '  - %s (%s): page 1..%s\n' \
      "${BOARD_NAMES[$i]}" "${BOARD_IDS[$i]}" "${TARGET_PAGES[$i]}"
  done
  cat <<'EOF'

Concurrency boundary
  Knowledge sync, Nexon collection, and Inven collection run concurrently.
  The seven Inven boards share one serial, one-request-per-second client path; they
  are intentionally not launched as seven processes because that would multiply
  requests to the same host and bypass the crawler's respectful pacing contract.
EOF
}

if (( $# > 1 )); then
  usage >&2
  exit 2
fi
case "${1:-}" in
  --plan)
    print_plan
    exit 0
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  "") ;;
  *)
    usage >&2
    exit 2
    ;;
esac

# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

page_batch="${RECOVERY_PAGE_BATCH:-10}"
crawl_retries="${RECOVERY_CRAWL_RETRIES:-5}"
crawl_retry_delay="${RECOVERY_CRAWL_RETRY_DELAY:-30}"
index_batch_size="${RECOVERY_INDEX_BATCH_SIZE:-32}"
embedding_provider="${EMBEDDING_PROVIDER:-local}"
embedding_device="${RECOVERY_EMBEDDING_DEVICE:-cuda}"
patch_days="${RECOVERY_PATCH_DAYS:-365}"
patch_max_pages="${RECOVERY_PATCH_MAX_PAGES:-60}"
backup_enabled="${RECOVERY_BACKUP:-true}"
recovery_lock_file="${RECOVERY_LOCK_FILE:-/tmp/maple-chat-production-recovery.lock}"
crawl_lock_file="${LIVE_CRAWL_LOCK_FILE:-/tmp/maple-chat-live-crawl.lock}"
patch_lock_file="${PATCH_NOTES_LOCK_FILE:-/tmp/maple-chat-patch-notes.lock}"
gpu_manager="${MAPLE_CHAT_LLM_MANAGER:-/home/hyuns/local-claude-code/docker-compose-manager.sh}"

require_uint() {
  local name="$1" value="$2" maximum="$3"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > maximum )); then
    echo "$name must be an integer between 1 and $maximum" >&2
    exit 2
  fi
}

require_uint RECOVERY_PAGE_BATCH "$page_batch" 100
require_uint RECOVERY_CRAWL_RETRIES "$crawl_retries" 20
require_uint RECOVERY_CRAWL_RETRY_DELAY "$crawl_retry_delay" 3600
require_uint RECOVERY_INDEX_BATCH_SIZE "$index_batch_size" 256
require_uint RECOVERY_PATCH_DAYS "$patch_days" 3650
require_uint RECOVERY_PATCH_MAX_PAGES "$patch_max_pages" 1000
if [[ "$embedding_device" != "cuda" && "$embedding_device" != "cpu" ]]; then
  echo "RECOVERY_EMBEDDING_DEVICE must be cuda or cpu" >&2
  exit 2
fi
if [[ "$embedding_provider" != "local" && "$embedding_provider" != "remote" ]]; then
  echo "EMBEDDING_PROVIDER must be local or remote" >&2
  exit 2
fi
if [[ "$backup_enabled" != "true" && "$backup_enabled" != "false" ]]; then
  echo "RECOVERY_BACKUP must be true or false" >&2
  exit 2
fi

mkdir -p "$ROOT_DIR/logs/recovery"
run_id="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="$ROOT_DIR/logs/recovery/$run_id"
mkdir -p "$run_dir"
ln -sfn "$run_dir" "$ROOT_DIR/logs/recovery/latest"
exec > >(tee -a "$run_dir/recovery.log") 2>&1

on_exit() {
  local rc=$?
  if (( rc == 0 )); then
    echo "[$(date --iso-8601=seconds)] Recovery completed successfully."
  else
    echo "[$(date --iso-8601=seconds)] Recovery stopped with exit code $rc."
    echo "Rerun the same command; committed board checkpoints make the crawl resumable."
  fi
  echo "Logs: $run_dir"
}
trap on_exit EXIT

exec 8>"$recovery_lock_file"
if ! flock -n 8; then
  echo "Another production recovery already holds $recovery_lock_file" >&2
  exit 3
fi

echo "[$(date --iso-8601=seconds)] Starting production data recovery"
print_plan

# Validate the network approval before starting a crawl or making any recovery write.
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

backup_database() {
  local label="$1"
  if [[ "$backup_enabled" != "true" ]]; then
    echo "Backup disabled by RECOVERY_BACKUP=false"
    return 0
  fi
  echo "Creating $label recovery backup (vectors included)"
  BACKUP_DIR="$ROOT_DIR/backups/recovery" \
    BACKUP_INCLUDE_VECTORS=true \
    "$ROOT_DIR/scripts/backup_postgres.sh" daily
}

backup_database pre

echo "Removing only the known integration-test fixture, if it is present"
compose_local exec -T postgres psql -X -v ON_ERROR_STOP=1 -U maple_chat -d maple_chat <<'SQL'
BEGIN;
CREATE TEMP TABLE recovery_fixture_answers ON COMMIT DROP AS
SELECT DISTINCT a.answer_id
FROM answers AS a
JOIN answer_sources AS s ON s.answer_id = a.answer_id
JOIN chunks AS c ON c.chunk_id = s.chunk_id
WHERE c.chunk_id = repeat('c', 64)
  AND c.source_key = 'article:2304:1'
  AND c.text = '정답 근거 본문'
  AND a.guild_id = 1
  AND a.channel_id = 10
  AND a.requester_hash = 'requester-hash';

DELETE FROM answer_sources
WHERE answer_id IN (SELECT answer_id FROM recovery_fixture_answers);
DELETE FROM answer_knowledge_sources
WHERE answer_id IN (SELECT answer_id FROM recovery_fixture_answers);
DELETE FROM answers
WHERE answer_id IN (SELECT answer_id FROM recovery_fixture_answers);
DELETE FROM chunks
WHERE chunk_id = repeat('c', 64)
  AND source_key = 'article:2304:1'
  AND text = '정답 근거 본문';
COMMIT;
SQL

checkpoint() {
  local board_id="$1" row
  row="$(compose_local exec -T postgres psql -X -qAt -F '|' \
    -U maple_chat -d maple_chat -c \
    "SELECT COALESCE(crawl_checkpoint->>'next_backfill_page', '1'),
            COALESCE(crawl_checkpoint->>'backfill_complete', 'false')
       FROM boards WHERE board_id = $board_id")"
  if [[ -z "$row" ]]; then
    printf '1|false\n'
  else
    printf '%s\n' "$row"
  fi
}

recover_community() {
  local pending i board_id board_name target next_page complete pages old_next new_state new_next
  local slice_end attempt attempt_start_page rc delay resumed_next resumed_complete
  while true; do
    pending=false
    for i in "${!BOARD_IDS[@]}"; do
      board_id="${BOARD_IDS[$i]}"
      board_name="${BOARD_NAMES[$i]}"
      target="${TARGET_PAGES[$i]}"
      IFS='|' read -r next_page complete < <(checkpoint "$board_id")
      if [[ "$complete" == "true" ]] || (( next_page > target )); then
        continue
      fi
      pending=true
      pages="$page_batch"
      if (( next_page + pages - 1 > target )); then
        pages=$((target - next_page + 1))
      fi
      old_next="$next_page"
      slice_end=$((next_page + pages - 1))
      attempt=1
      while true; do
        IFS='|' read -r next_page complete < <(checkpoint "$board_id")
        if [[ "$complete" == "true" ]] || (( next_page > slice_end )); then
          break
        fi
        pages=$((slice_end - next_page + 1))
        attempt_start_page="$next_page"
        echo "[$(date --iso-8601=seconds)] $board_name($board_id): pages $next_page-$slice_end (attempt $attempt/$crawl_retries)"
        if conda_run maple-chat crawl-live \
          --scope full_backfill \
          --board "$board_id" \
          --start-page "$next_page" \
          --max-pages "$pages" \
          --max-articles 100 \
          --resume \
          --no-ocr; then
          break
        else
          rc=$?
        fi
        IFS='|' read -r resumed_next resumed_complete < <(checkpoint "$board_id")
        if [[ "$resumed_complete" == "true" ]] || (( resumed_next > slice_end )); then
          break
        fi
        if (( resumed_next > attempt_start_page )); then
          echo "Board $board_id advanced to page $resumed_next before the transient failure; resetting the consecutive retry counter" >&2
          attempt=1
        elif (( attempt >= crawl_retries )); then
          echo "Board $board_id page $attempt_start_page failed $attempt consecutive times (last exit $rc)" >&2
          return "$rc"
        fi
        delay=$((crawl_retry_delay * attempt))
        echo "Transient crawl failure on board $board_id; retrying from its committed checkpoint in ${delay}s" >&2
        sleep "$delay"
        if (( resumed_next <= attempt_start_page )); then
          attempt=$((attempt + 1))
        fi
      done
      new_state="$(checkpoint "$board_id")"
      IFS='|' read -r new_next complete <<<"$new_state"
      if [[ "$complete" != "true" ]] && (( new_next <= old_next )); then
        echo "Board $board_id checkpoint did not advance from page $old_next" >&2
        return 5
      fi
    done
    if [[ "$pending" == "false" ]]; then
      return 0
    fi
  done
}

sync_knowledge() {
  conda_run maple-chat sync-knowledge
}

sync_patch_notes_without_index() {
  conda_run maple-chat sync-patch-notes \
    --since-days "$patch_days" \
    --max-pages "$patch_max_pages" \
    --no-index
}

echo "Launching Knowledge Graph, Nexon, and Inven recovery lanes"
(sync_knowledge) >"$run_dir/knowledge.log" 2>&1 &
knowledge_pid=$!
(
  exec 9>"$patch_lock_file"
  flock 9
  sync_patch_notes_without_index
) >"$run_dir/patch-notes.log" 2>&1 &
patch_pid=$!
(
  exec 9>"$crawl_lock_file"
  flock 9
  recover_community
) >"$run_dir/community.log" 2>&1 &
community_pid=$!

stage_failure=0
for stage_pid in \
  "knowledge:$knowledge_pid" \
  "patch-notes:$patch_pid" \
  "community:$community_pid"; do
  stage="${stage_pid%%:*}"
  pid="${stage_pid##*:}"
  if wait "$pid"; then
    echo "[$(date --iso-8601=seconds)] Stage completed: $stage"
  else
    rc=$?
    stage_failure=1
    echo "[$(date --iso-8601=seconds)] Stage failed: $stage (exit $rc)" >&2
    tail -n 40 "$run_dir/$stage.log" >&2 || true
  fi
done
if (( stage_failure != 0 )); then
  echo "At least one collection stage failed; indexing was not started." >&2
  exit 5
fi

echo "All source collection completed; starting the single bulk BGE-M3 indexing pass"
export EMBEDDING_PROVIDER="$embedding_provider"
export EMBEDDING_DEVICE="$embedding_device"
if [[ "$embedding_provider" == "remote" ]]; then
  conda_run maple-chat index --batch-size "$index_batch_size"
elif [[ "$embedding_device" == "cuda" && -x "$gpu_manager" ]]; then
  "$gpu_manager" gpu-job run \
    --drain-timeout "${RECOVERY_LLM_DRAIN_TIMEOUT:-1800}" \
    --ready-timeout "${RECOVERY_LLM_READY_TIMEOUT:-1800}" \
    -- "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
      maple-chat index --batch-size "$index_batch_size"
else
  if [[ "$embedding_device" == "cuda" ]] && \
    curl -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8001/health >/dev/null 2>&1; then
    echo "A local LLM is using GPU, but no gpu-job manager was found at $gpu_manager" >&2
    echo "Stop the local LLM or set MAPLE_CHAT_LLM_MANAGER before rerunning." >&2
    exit 6
  fi
  conda_run maple-chat index --batch-size "$index_batch_size"
fi

echo "Running the post-index terminology audit"
conda_run maple-chat audit-knowledge >>"$run_dir/knowledge-audit.jsonl"

echo "Verifying exact Knowledge Graph counts and recovered data invariants"
conda_run python "$ROOT_DIR/scripts/verify_recovery_state.py" \
  | tee "$run_dir/verification.json"

backup_database post
