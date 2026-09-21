#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

if [[ -z "${DISCORD_CHANNEL_IDS:-}" ]]; then
  echo "DISCORD_CHANNEL_IDS must contain at least one test channel ID" >&2
  exit 2
fi
cd "$ROOT_DIR"
conda_run maple-chat validate-config --role bot
if [[ "${RUN_LIVE_SYNC_ON_START:-true}" == "true" ]]; then
  conda_run maple-chat validate-config --role crawler-worker
fi

start_database

if [[ "${RUN_LIVE_SYNC_ON_START:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/sync_live.sh"
fi

"$ROOT_DIR/scripts/start_local_model.sh"
conda_run maple-chat preflight

daily_sync_pid=""
weekly_patch_sync_pid=""
cleanup() {
  if [[ -n "$daily_sync_pid" ]]; then
    kill "$daily_sync_pid" >/dev/null 2>&1 || true
  fi
  if [[ -n "$weekly_patch_sync_pid" ]]; then
    kill "$weekly_patch_sync_pid" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM
if [[ "${RUN_DAILY_SYNC:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/daily_sync_loop.sh" &
  daily_sync_pid=$!
fi
if [[ "${RUN_WEEKLY_PATCH_SYNC:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/weekly_patch_notes_loop.sh" &
  weekly_patch_sync_pid=$!
fi

if [[ "${BOT_DISABLE_CUDA:-false}" == "true" ]]; then
  # Keep the in-process BGE cross-encoder on CPU: the GPU is already held by the
  # local serving stack, and embeddings are served by the remote TEI server.
  export CUDA_VISIBLE_DEVICES=""
fi
conda_run maple-chat run-bot
