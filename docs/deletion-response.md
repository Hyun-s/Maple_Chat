# Deletion and Rights-Complaint Response

1. Verify the requested Inven URL and normalize it to its canonical source key.
2. The owner-only command writes an audited active denylist entry.
3. In one transaction mark the article denied/deleted, remove the sanitized body if deletion is
   observed, deactivate every related chunk and vector, and upsert a tombstone.
4. Preserve prior `answer_sources` only as an immutable audit link. Source replay marks the item
   deleted and suppresses its URL; retrieval and new generation cannot select inactive chunks.
5. Startup reconciliation reapplies every active denylist entry, so a cache/process restart cannot
   revive excluded material. Maple Chat has no durable raw-content or image cache.
6. Record the actor hash, reason, target, and result without storing requester PII in logs.
