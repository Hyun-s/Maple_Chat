# Deletion and Rights-Complaint Response

1. Verify the requested Inven URL and normalize it to its canonical source key.
2. `exclude_article` writes the audited active denylist entry in the same transaction that marks
   the source. Today it is reached from the live crawler path (`reason="source article is
   unavailable"`); there is no owner-only admin command in the tree.
3. In one transaction mark the article denied/deleted, remove the sanitized body if deletion is
   observed, deactivate every related chunk and vector, and upsert a tombstone.
4. Preserve prior `answer_sources` only as an immutable audit link. Source replay marks the item
   deleted and suppresses its URL; retrieval and new generation cannot select inactive chunks.
5. Every article write path is `upsert_article_with_outbox`, and it raises `SourceDenied` when an
   active denylist entry already covers that source key. The block is therefore enforced by the
   write path itself, not by a startup sweep, so a process restart or a fresh crawl cannot revive
   excluded material. Maple Chat has no durable raw-content or image cache.
6. Record the actor hash, reason, target, and result without storing requester PII in logs.
