# G004 Verification Evidence

- Article, comment branch, and OCR-compatible chunk contracts use stable IDs, structural
  boundaries, 400–700-token targets, at most 80-token overlap, and revision metadata.
- BGE-M3 requires an immutable model commit, normalized 1024-dimensional cosine vectors, and
  idempotent `(chunk_id, revision)` writes.
- Local sentence-transformer adapters pin both `BAAI/bge-m3` and
  `BAAI/bge-reranker-v2-m3` to immutable revisions, refuse remote model download at runtime, and
  move synchronous CPU inference off the asyncio event loop.
- PostgreSQL integration proves HNSW dense and pg_trgm lexical retrieval, RRF, bounded
  quality/recency boosts, deterministic reranking, source diversity, revision cutover, inactive
  filtering, and low-evidence refusal.
- Retrieved content remains serialized inside an explicit untrusted-data prompt boundary.
