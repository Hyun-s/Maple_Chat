#!/usr/bin/env bash

# Index pending sanitized sources while temporarily yielding the GPU from vLLM.

set +x
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

batch_size="${INDEX_BATCH_SIZE:-32}"
embedding_provider="${EMBEDDING_PROVIDER:-local}"
embedding_device="${INDEX_EMBEDDING_DEVICE:-cuda}"
index_lock_file="${INDEX_LOCK_FILE:-/tmp/maple-chat-index.lock}"
gpu_manager="${MAPLE_CHAT_LLM_MANAGER:-/home/hyuns/local-claude-code/docker-compose-manager.sh}"

if ! [[ "$batch_size" =~ ^[0-9]+$ ]] || (( batch_size < 1 || batch_size > 256 )); then
  echo "INDEX_BATCH_SIZE must be an integer between 1 and 256" >&2
  exit 2
fi
if [[ "$embedding_device" != "cuda" && "$embedding_device" != "cpu" ]]; then
  echo "INDEX_EMBEDDING_DEVICE must be cuda or cpu" >&2
  exit 2
fi
if [[ "$embedding_provider" != "local" && "$embedding_provider" != "remote" ]]; then
  echo "EMBEDDING_PROVIDER must be local or remote" >&2
  exit 2
fi

start_database
exec 8>"$index_lock_file"
flock 8

pending="$(compose_local exec -T postgres psql -X -qAt \
  -U maple_chat -d maple_chat -c \
  "SELECT
       (SELECT count(*) FROM articles WHERE status = 'sanitized') +
       (SELECT count(*) FROM comments WHERE status = 'sanitized') +
       (SELECT count(*) FROM media_assets WHERE fetch_status = 'ocr_ready')")"
if [[ "$pending" == "0" ]]; then
  echo "No sanitized sources are waiting for indexing"
  exit 0
fi

echo "Indexing $pending pending sanitized sources with BGE-M3 via $embedding_provider provider"
export EMBEDDING_PROVIDER="$embedding_provider"
export EMBEDDING_DEVICE="$embedding_device"
if [[ "$embedding_provider" == "remote" ]]; then
  conda_run maple-chat index --batch-size "$batch_size"
elif [[ "$embedding_device" == "cuda" && -x "$gpu_manager" ]]; then
  "$gpu_manager" gpu-job run \
    --drain-timeout "${INDEX_LLM_DRAIN_TIMEOUT:-1800}" \
    --ready-timeout "${INDEX_LLM_READY_TIMEOUT:-1800}" \
    -- "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
      maple-chat index --batch-size "$batch_size"
else
  if [[ "$embedding_device" == "cuda" ]] && \
    curl -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8001/health >/dev/null 2>&1; then
    echo "A local LLM is using GPU, but no gpu-job manager was found at $gpu_manager" >&2
    exit 6
  fi
  conda_run maple-chat index --batch-size "$batch_size"
fi
