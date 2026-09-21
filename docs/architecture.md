# G001 Architecture Baseline

Maple Chat is one Python package deployed as four isolated process roles:

- `bot`: private-guild Discord input and answer delivery
- `scheduler`: durable synchronization job creation
- `crawler-worker`: allowlisted collection through the rights-aware request gate
- `index-worker`: sanitised document chunking, OCR, embedding, and indexing

PostgreSQL 16 with pgvector and pg_trgm is the only planned durable database and job queue.
Kafka, Redis, Elasticsearch, Milvus, and cloud LLM services are outside the approved scope.

## Trust boundaries

1. Inven content, OCR text, external link titles, and Discord input are untrusted data.
2. PII sanitation must occur before durable storage or embedding.
3. `DISCORD_TOKEN`, `DATABASE_URL`, and `PII_HASH_SALT` remain environment-only secrets.
   Compose injects Discord credentials only into `bot` and the PII salt only into the crawler
   and index workers that require it.
4. The database has only an internal Compose network and no host port mapping.
5. App containers are non-root, read-only, capability-free, and expose no inbound ports.
6. `/home/hyuns/local-claude-code` owns the local vLLM lifecycle; Maple Chat only connects to
   the configured local OpenAI-compatible endpoint.
7. Live crawling and public release are independent fail-closed gates.

The implemented pipeline adds the canonical PostgreSQL schema, transactional outbox, idempotent
repositories, fixture-first sanitization, single-active embedding revisions, hybrid retrieval,
immutable answer sources, seven-board scheduling, and lease-based durable jobs without changing
these boundaries.
