#!/usr/bin/env bash
set -euo pipefail

backup=${1:?usage: restore_postgres.sh BACKUP_FILE TARGET_DB}
target_db=${2:?usage: restore_postgres.sh BACKUP_FILE TARGET_DB}
if [[ ! -f $backup ]]; then
  echo "backup file does not exist" >&2
  exit 2
fi
if [[ ! $target_db =~ ^maple_chat_restore_[a-zA-Z0-9_]+$ ]]; then
  echo "target DB must use the isolated maple_chat_restore_* prefix" >&2
  exit 2
fi

docker compose exec -T postgres createdb -U maple_chat --template=template0 "$target_db"
target_url="postgresql+asyncpg://maple_chat:${POSTGRES_PASSWORD:-}@postgres:5432/${target_db}"
docker compose run --rm --no-deps \
  -e DATABASE_URL="$target_url" \
  --entrypoint alembic scheduler upgrade head
docker compose exec -T postgres pg_restore \
  -U maple_chat -d "$target_db" \
  --data-only --single-transaction --exit-on-error <"$backup"

docker compose exec -T postgres psql -U maple_chat -d "$target_db" -v ON_ERROR_STOP=1 <<'SQL'
UPDATE crawl_jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'queued' END,
    lease_owner = NULL,
    leased_until = NULL,
    last_error_code = 'restore_reconciliation',
    updated_at = now()
WHERE state = 'leased';

DO $$
BEGIN
  IF (SELECT count(*) FROM embedding_revisions WHERE active) > 1 THEN
    RAISE EXCEPTION 'multiple active embedding revisions after restore';
  END IF;
END $$;
SQL

printf 'restored and reconciled database: %s\n' "$target_db"
