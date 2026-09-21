#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

cd "$ROOT_DIR"
start_database
conda_run maple-chat sync-patch-notes \
  --since-days "${PATCH_NOTES_RETENTION_DAYS:-365}" \
  --max-pages "${PATCH_NOTES_MAX_LIST_PAGES:-60}" \
  --no-index
INDEX_BATCH_SIZE="${PATCH_NOTES_INDEX_BATCH_SIZE:-32}" \
  "$ROOT_DIR/scripts/index_with_gpu_handoff.sh"
