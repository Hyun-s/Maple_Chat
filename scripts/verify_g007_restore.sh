#!/usr/bin/env bash
set -euo pipefail

name="maple-chat-restore-$PPID-$$"
backup=$(mktemp --suffix=.dump)
cleanup() {
  docker stop "$name" >/dev/null 2>&1 || true
  docker container rm "$name" >/dev/null 2>&1 || true
  rm -f "$backup"
}
trap cleanup EXIT

docker run -d --name "$name" \
  -e POSTGRES_DB=maple_chat \
  -e POSTGRES_USER=maple_chat \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1::5432 \
  pgvector/pgvector:0.8.0-pg16 >/dev/null
for _ in $(seq 1 30); do
  docker exec "$name" pg_isready -U maple_chat -d maple_chat >/dev/null 2>&1 && break
  sleep 1
done
port=$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')
source_url="postgresql+asyncpg://maple_chat@127.0.0.1:${port}/maple_chat"
restore_db="maple_chat_restore_drill"
restore_url="postgresql+asyncpg://maple_chat@127.0.0.1:${port}/${restore_db}"

DATABASE_URL="$source_url" alembic upgrade head
docker exec -i "$name" psql -U maple_chat -d maple_chat -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO boards (board_id, name) VALUES (2304, 'restore fixture');
INSERT INTO embedding_revisions
  (name, model, model_commit, dimension, normalized, distance, activated_at, active)
VALUES ('restore-v1', 'BAAI/bge-m3', 'fixture-commit', 1024, true, 'cosine', now(), true);
INSERT INTO crawl_jobs
  (job_type, dedupe_key, payload, state, attempts, max_attempts, lease_owner, leased_until)
VALUES ('restore', 'restore-job', '{}', 'leased', 1, 5, 'dead-worker', now() - interval '1 hour');
SQL

docker exec "$name" pg_dump -U maple_chat -d maple_chat \
  --data-only --format=custom --no-owner --no-acl \
  --exclude-table-data=alembic_version --exclude-table-data=chunk_embeddings >"$backup"
docker exec -i "$name" pg_restore --list <"$backup" >/dev/null
docker exec "$name" createdb -U maple_chat --template=template0 "$restore_db"
DATABASE_URL="$restore_url" alembic upgrade head
docker exec -i "$name" pg_restore -U maple_chat -d "$restore_db" \
  --data-only --single-transaction --exit-on-error <"$backup"
docker exec -i "$name" psql -U maple_chat -d "$restore_db" -v ON_ERROR_STOP=1 <<'SQL'
UPDATE crawl_jobs
SET state = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'queued' END,
    lease_owner = NULL,
    leased_until = NULL,
    last_error_code = 'restore_reconciliation',
    updated_at = now()
WHERE state = 'leased';
SQL

test "$(docker exec "$name" psql -U maple_chat -d "$restore_db" -Atc 'SELECT count(*) FROM boards')" = 1
test "$(docker exec "$name" psql -U maple_chat -d "$restore_db" -Atc 'SELECT count(*) FROM embedding_revisions WHERE active')" = 1
test "$(docker exec "$name" psql -U maple_chat -d "$restore_db" -Atc "SELECT count(*) FROM crawl_jobs WHERE state='queued' AND lease_owner IS NULL")" = 1
printf 'temporary-database restore drill passed\n'
