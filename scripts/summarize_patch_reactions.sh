#!/usr/bin/env bash

# Wait for the current all-board crawl/index run, then summarize each job sequentially.

set +x
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

if (( $# > 0 )) && [[ "$1" != --* ]]; then
  patch_date="$1"
  shift
else
  patch_date="$(TZ=Asia/Seoul date +%F)"
fi
output_dir="${PATCH_REACTION_OUTPUT_DIR:-$ROOT_DIR/reports/patch-reactions}"
crawl_lock_file="${LIVE_CRAWL_LOCK_FILE:-/tmp/maple-chat-live-crawl.lock}"

if ! [[ "$patch_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "patch date must use YYYY-MM-DD" >&2
  exit 2
fi

start_database

# The daily job holds this lock through ingestion and indexing. Acquiring and
# immediately releasing it gives this report a stable, fully indexed snapshot.
exec 8>"$crawl_lock_file"
echo "Waiting for the current community sync to finish..."
flock 8
flock -u 8

"$ROOT_DIR/scripts/start_local_model.sh"
conda_run maple-chat summarize-patch-reactions \
  --date "$patch_date" \
  --output-dir "$output_dir" \
  --evidence-limit "${PATCH_REACTION_EVIDENCE_LIMIT:-8}" \
  --max-comments-per-article "${PATCH_REACTION_COMMENT_LIMIT:-12}" \
  "$@"
