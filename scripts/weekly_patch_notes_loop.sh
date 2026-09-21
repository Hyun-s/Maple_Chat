#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

patch_lock_file="${PATCH_NOTES_LOCK_FILE:-/tmp/maple-chat-patch-notes.lock}"
schedule_lock_file="${PATCH_NOTES_SCHEDULER_LOCK_FILE:-/tmp/maple-chat-patch-scheduler.lock}"
exec 8>"$schedule_lock_file"
if ! flock -n 8; then
  echo "Weekly patch-note scheduler is already running"
  exit 0
fi

while true; do
  delay="$({ PATCH_NOTES_WEEKLY_AT="${PATCH_NOTES_WEEKLY_AT:-05:00}" conda_run python -c '
import datetime as dt
import os

from maple_chat.scheduler.planner import next_weekly_patch_run

now = dt.datetime.now(dt.UTC)
target = next_weekly_patch_run(os.environ["PATCH_NOTES_WEEKLY_AT"], now)
print(max(1, int((target - now).total_seconds())))
  '; } 2>/dev/null)"
  echo "[$(date --iso-8601=seconds)] Next weekly patch-note sync in ${delay}s"
  sleep "$delay"
  if ! (
    flock 9
    "$ROOT_DIR/scripts/sync_patch_notes.sh"
  ) 9>"$patch_lock_file"; then
    echo "Weekly official patch-note sync failed safely; the Discord bot remains online" >&2
  fi
done
