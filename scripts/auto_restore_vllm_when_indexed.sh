#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/_local_env.sh"

log_file="$ROOT_DIR/logs/auto-restore-vllm.log"
mkdir -p "$ROOT_DIR/logs"

echo "[$(date --iso-8601=seconds)] Auto-restorer started. Monitoring indexing completion..." | tee -a "$log_file"

while true; do
  pending=$(compose_local exec -T postgres psql -X -qAt -U maple_chat -d maple_chat -c \
    "SELECT (SELECT count(*) FROM articles WHERE status = 'sanitized') + (SELECT count(*) FROM comments WHERE status = 'sanitized')" 2>/dev/null || echo "error")
  
  if [[ "$pending" == "0" ]]; then
    echo "[$(date --iso-8601=seconds)] All sources indexed (pending=0)! Restoring qwen38-flash-next..." | tee -a "$log_file"
    /home/hyuns/local-claude-code/docker-compose-manager.sh up qwen38-flash-next >> "$log_file" 2>&1
    echo "[$(date --iso-8601=seconds)] qwen38-flash-next successfully restored!" | tee -a "$log_file"
    break
  fi
  sleep 30
done
