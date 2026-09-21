#!/usr/bin/env bash

# Never allow caller-provided xtrace to print inherited secrets.
set +x
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_BIN="${CONDA_BIN:-/home/hyuns/anaconda3/bin/conda}"
CONDA_ENV="${CONDA_ENV:-maple-chat}"

declare -A inherited_values=()
for key in \
  DISCORD_TOKEN DISCORD_GUILD_ID DISCORD_GUILD_IDS DISCORD_OWNER_ID DISCORD_CHANNEL_IDS \
  DATABASE_URL POSTGRES_PASSWORD PII_HASH_SALT CRAWLER_USER_AGENT CRAWLER_CONTACT \
  LIVE_CRAWL_ENABLED LIVE_CRAWL_APPROVAL_FILE NEXON_API_KEY NEXON_ANALYTICS_SCRIPT \
  EMBEDDING_PROVIDER EMBEDDING_BASE_URL EMBEDDING_REMOTE_MODEL EMBEDDING_TIMEOUT_SECONDS; do
  if [[ -n "${!key:-}" ]]; then
    inherited_values["$key"]="${!key}"
  fi
done
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi
for key in "${!inherited_values[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    printf -v "$key" '%s' "${inherited_values[$key]}"
    export "$key"
  fi
done

if [[ ! -x "$CONDA_BIN" ]]; then
  echo "Conda executable not found: $CONDA_BIN" >&2
  exit 2
fi
if ! "$CONDA_BIN" env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
  echo "Conda environment '$CONDA_ENV' is missing. Run: conda env create -f environment.yml" >&2
  exit 2
fi

POSTGRES_PORT="${POSTGRES_PORT:-55432}"
export POSTGRES_PORT
if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "POSTGRES_PASSWORD must be set in .env" >&2
  exit 2
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  encoded_password="$($CONDA_BIN run -n "$CONDA_ENV" python -c \
    'import os, urllib.parse; print(urllib.parse.quote(os.environ["POSTGRES_PASSWORD"], safe=""))')"
  DATABASE_URL="postgresql+asyncpg://maple_chat:${encoded_password}@127.0.0.1:${POSTGRES_PORT}/maple_chat"
  export DATABASE_URL
fi

compose_local() {
  docker compose -f "$ROOT_DIR/compose.yml" -f "$ROOT_DIR/compose.local.yml" "$@"
}

conda_run() {
  "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" "$@"
}

start_database() {
  compose_local up -d postgres
  for _ in $(seq 1 60); do
    if compose_local exec -T postgres pg_isready -U maple_chat -d maple_chat >/dev/null 2>&1; then
      (cd "$ROOT_DIR" && conda_run alembic upgrade head)
      return 0
    fi
    sleep 1
  done
  echo "PostgreSQL did not become ready within 60 seconds" >&2
  return 1
}
