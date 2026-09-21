#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

schedule_lock_file="${DAILY_SYNC_SCHEDULER_LOCK_FILE:-/tmp/maple-chat-daily-scheduler.lock}"
exec 8>"$schedule_lock_file"
if ! flock -n 8; then
  echo "Daily incremental scheduler is already running"
  exit 0
fi

while true; do
  delay="$({ CRAWL_DAILY_AT="${CRAWL_DAILY_AT:-05:00}" conda_run python -c '
import datetime as dt
import os
from zoneinfo import ZoneInfo

hour, minute = (int(value) for value in os.environ["CRAWL_DAILY_AT"].split(":"))
now = dt.datetime.now(ZoneInfo("Asia/Seoul"))
target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
if target <= now:
    target += dt.timedelta(days=1)
print(max(1, int((target - now).total_seconds())))
  '; } 2>/dev/null)"
  echo "[$(date --iso-8601=seconds)] Next daily incremental sync in ${delay}s"
  sleep "$delay"
  mkdir -p "$ROOT_DIR/logs"
  if ! "$ROOT_DIR/scripts/daily_incremental_sync.sh" \
    >>"$ROOT_DIR/logs/daily-incremental.log" 2>&1; then
    echo "Daily live sync failed safely; the Discord bot remains online" >&2
  fi
done
