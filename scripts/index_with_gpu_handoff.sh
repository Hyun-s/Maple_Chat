#!/usr/bin/env bash

# Index pending sanitized sources while temporarily yielding the GPU from vLLM.

set +x
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"
cd "$ROOT_DIR"

batch_size="${INDEX_BATCH_SIZE:-32}"
embedding_provider="${EMBEDDING_PROVIDER:-remote}"
index_lock_file="${INDEX_LOCK_FILE:-/tmp/maple-chat-index.lock}"
max_batches_per_source="${INDEX_MAX_BATCHES_PER_SOURCE:-8}"
gpu_manager="${MAPLE_CHAT_LLM_MANAGER:-/home/hyuns/local-claude-code/docker-compose-manager.sh}"

if ! [[ "$batch_size" =~ ^[0-9]+$ ]] || (( batch_size < 1 || batch_size > 256 )); then
  echo "INDEX_BATCH_SIZE must be an integer between 1 and 256" >&2
  exit 2
fi
if ! [[ "$max_batches_per_source" =~ ^[0-9]+$ ]]; then
  echo "INDEX_MAX_BATCHES_PER_SOURCE must be a non-negative integer" >&2
  exit 2
fi
if [[ "$embedding_provider" != "remote" ]]; then
  echo "EMBEDDING_PROVIDER must be remote: the in-process BGE-M3 model was removed," >&2
  echo "so '$embedding_provider' aborts inside maple-chat index anyway." >&2
  exit 2
fi

start_database
exec 8>"$index_lock_file"
flock 8

count_pending() {
  compose_local exec -T postgres psql -X -qAt \
    -U maple_chat -d maple_chat -c \
    "SELECT
       (SELECT count(*) FROM articles WHERE status = 'sanitized') +
       (SELECT count(*) FROM comments WHERE status = 'sanitized') +
       (SELECT count(*) FROM media_assets WHERE fetch_status = 'ocr_ready')"
}

pending="$(count_pending)"
if [[ "$pending" == "0" ]]; then
  echo "No sanitized sources are waiting for indexing"
  exit 0
fi

echo "Indexing $pending pending sanitized sources with BGE-M3 via $embedding_provider provider"
export EMBEDDING_PROVIDER="$embedding_provider"

vllm_was_running="false"
if docker ps --format '{{.Names}}' | grep -qx "vllm-local-coder"; then
  echo "Local model is running; pausing it to yield GPU memory to TEI..."
  vllm_was_running="true"
  if [[ -x "$gpu_manager" ]]; then
    "$gpu_manager" down mia-qwen >/dev/null 2>&1 || docker stop vllm-local-coder >/dev/null 2>&1 || true
  else
    docker stop vllm-local-coder >/dev/null 2>&1 || true
  fi
  if [[ -x "$gpu_manager" ]]; then
    "$gpu_manager" encoders restart >/dev/null 2>&1 || true
  fi
fi

restore_vllm() {
  if [[ "$vllm_was_running" == "true" ]]; then
    echo "Indexing finished; restoring local model (mia-qwen)..."
    if [[ -x "$gpu_manager" ]]; then
      "$gpu_manager" up mia-qwen >/dev/null 2>&1 || true
    fi
  fi
}
trap restore_vllm EXIT INT TERM

if [[ "$embedding_provider" == "remote" ]]; then
  # A degraded TEI encoder keeps /health green and only fails on chunk-length
  # inputs, so name the cause here instead of aborting with an opaque 424.
  "$ROOT_DIR/scripts/verify_model_plane.sh"
  index_attempts="${INDEX_ATTEMPTS:-5}"
  # Articles are indexed before comments, so an unbounded run that dies on a TEI
  # overload error can spend its whole life inside the article loop and never
  # embed a single comment. Bounding each pass keeps all three source types
  # moving, and the backlog re-check below keeps the pass rate steady.
  while true; do
    attempt=1
    while true; do
      # Indexing commits per batch and re-selects pending rows with SKIP LOCKED,
      # so a retry after an httpx timeout resumes instead of duplicating vectors.
      if conda_run maple-chat index --batch-size "$batch_size" \
        --max-batches-per-source "$max_batches_per_source"; then
        break
      fi
      if (( attempt >= index_attempts )); then
        echo "Indexing failed after $attempt attempts; pending rows remain for the next run" >&2
        exit 7
      fi
      echo "Index attempt $attempt/$index_attempts failed; retrying in 20s" >&2
      sleep 20
      attempt=$((attempt + 1))
    done
    pending="$(count_pending)"
    echo "Indexing pass complete; $pending sanitized sources still pending"
    if [[ "$pending" == "0" ]]; then
      break
    fi
  done
fi
