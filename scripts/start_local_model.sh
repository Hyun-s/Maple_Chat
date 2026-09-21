#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8001/v1}"
LLM_MODEL="${LLM_MODEL:-local-coder}"
LLM_COMPOSE_FILE="${MAPLE_CHAT_LLM_COMPOSE_FILE:-/home/hyuns/local-claude-code/docker-compose.qwen38-27b.yml}"
LLM_COMPOSE_SERVICE="${MAPLE_CHAT_LLM_COMPOSE_SERVICE:-vllm}"
LLM_PROFILE="${MAPLE_CHAT_LLM_PROFILE:-qwen38-27b}"
LLM_PROJECT_DIR="$(dirname "$LLM_COMPOSE_FILE")"
LLM_MANAGER="${MAPLE_CHAT_LLM_MANAGER:-$LLM_PROJECT_DIR/docker-compose-manager.sh}"

model_ready() {
  LLM_BASE_URL="$LLM_BASE_URL" LLM_MODEL="$LLM_MODEL" conda_run python - <<'PY'
import json
import os
import urllib.request

try:
    with urllib.request.urlopen(os.environ["LLM_BASE_URL"].rstrip("/") + "/models", timeout=3) as response:
        payload = json.load(response)
    raise SystemExit(0 if any(row.get("id") == os.environ["LLM_MODEL"] for row in payload.get("data", [])) else 1)
except Exception:
    raise SystemExit(1)
PY
}

if model_ready; then
  echo "Local model is already ready at $LLM_BASE_URL"
  exit 0
fi
if [[ "$LLM_BASE_URL" != "http://127.0.0.1:8001/v1" && "$LLM_BASE_URL" != "http://localhost:8001/v1" ]]; then
  echo "Custom LLM_BASE_URL is not ready; automatic startup only owns loopback port 8001" >&2
  exit 3
fi
if [[ ! -f "$LLM_COMPOSE_FILE" ]]; then
  echo "Local model Compose file is missing: $LLM_COMPOSE_FILE" >&2
  exit 3
fi

if [[ -x "$LLM_MANAGER" ]]; then
  (cd "$LLM_PROJECT_DIR" && "$LLM_MANAGER" up "$LLM_PROFILE")
else
  docker compose -f "$LLM_COMPOSE_FILE" up -d "$LLM_COMPOSE_SERVICE"
fi

echo "Waiting for local model readiness (this can take several minutes)..."
for _ in $(seq 1 180); do
  if model_ready; then
    echo "Local model is ready at $LLM_BASE_URL"
    exit 0
  fi
  if ! docker compose -f "$LLM_COMPOSE_FILE" ps --status running --services \
    | grep -Fxq "$LLM_COMPOSE_SERVICE"; then
    echo "Local model service stopped unexpectedly. Inspect the Qwen Compose logs." >&2
    exit 4
  fi
  sleep 10
done
echo "Local model did not become ready within 30 minutes" >&2
exit 4
