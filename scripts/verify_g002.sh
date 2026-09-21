#!/usr/bin/env bash
set -euo pipefail

name="maple-chat-g002-$PPID-$$"
cleanup() {
  docker stop "$name" >/dev/null 2>&1 || true
  docker container rm "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$name" \
  -e POSTGRES_DB=maple_chat \
  -e POSTGRES_USER=maple_chat \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p 127.0.0.1::5432 \
  pgvector/pgvector:0.8.0-pg16 >/dev/null

for _ in $(seq 1 30); do
  if docker exec "$name" pg_isready -U maple_chat -d maple_chat >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$name" pg_isready -U maple_chat -d maple_chat >/dev/null

port=$(docker port "$name" 5432/tcp | head -n 1 | sed 's/.*://')
database_url="postgresql+asyncpg://maple_chat@127.0.0.1:${port}/maple_chat"

DATABASE_URL="$database_url" alembic upgrade head
DATABASE_URL="$database_url" alembic downgrade base
DATABASE_URL="$database_url" alembic upgrade head
G002_ALLOW_DESTRUCTIVE_TEST_DATABASE=1 G002_DATABASE_URL="$database_url" \
  pytest -q tests/unit tests/integration \
  --cov=maple_chat --cov-report=term-missing

docker exec "$name" psql -U maple_chat -d maple_chat -Atc \
  "SELECT count(*) FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')" \
  | grep -qx '2'
docker exec "$name" psql -U maple_chat -d maple_chat -Atc \
  "SELECT count(*) FROM pg_indexes WHERE indexname IN ('ix_chunk_embeddings_hnsw', 'ix_articles_body_trgm')" \
  | grep -qx '2'
