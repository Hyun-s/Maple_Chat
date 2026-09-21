# G002 Verification Evidence

Verified on 2026-08-04 in the dedicated Conda environment `maple-chat` against an ephemeral
loopback-only `pgvector/pgvector:0.8.0-pg16` database. The production Compose database remains
unpublished.

## Implemented contracts

- Canonical entities use UTC-aware timestamps and PostgreSQL `JSONB`, `ARRAY`, pgvector, and
  pg_trgm indexes.
- Alembic creates both required extensions before dependent indexes and supports a clean
  `upgrade -> downgrade base -> upgrade` rehearsal.
- Article/outbox and active-job writes are idempotent; active jobs use a partial unique index.
- Workers claim jobs with `FOR UPDATE SKIP LOCKED`, reclaim expired leases, apply bounded
  exponential retry, and enter `dead_letter` at the attempt limit.
- Embedding revision activation is transactionally serialized and permits one active revision.
- Tombstone/denylist exclusion deactivates source chunks and vectors in the same transaction.

## Automated evidence

```text
Alembic upgrade/downgrade/upgrade: passed
Required extensions: vector, pg_trgm
Required indexes: HNSW vector, GIN trigram
PostgreSQL integration: 4 passed
Unit tests: 26 passed
Ruff: passed
Mypy strict: passed
```

Run the complete database proof with `scripts/verify_g002.sh` from the activated Conda
environment. The script creates and removes only its uniquely named ephemeral test container.
