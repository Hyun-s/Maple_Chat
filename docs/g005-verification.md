# G005 Verification Evidence

- The OpenAI-compatible client accepts only loopback/approved local hosts, verifies
  `local-coder`, applies bounded retries/timeouts, and has no cloud fallback.
- The QA transaction serializes duplicate Discord message IDs, persists immutable
  `answer_sources`, refuses low evidence, and produces a labeled retrieval-only fallback on local
  LLM failure.
- Discord routing accepts only direct mentions in the configured guild/channel, rejects bot,
  webhook, blocked, oversized, and dangerous-scheme inputs, rate limits, splits long output, and
  neutralizes all mentions.
- The persistent `출처 보기` custom ID is tied to the original `answer_id`; requester hashes and
  TTL checks prevent cross-user/expired replay. Deleted sources are marked and their links hidden.
- Administration is owner-only and writes hashed-actor audit records.

Actual private-guild and external local-vLLM validation remains a release artifact, not a CI
fixture claim.
