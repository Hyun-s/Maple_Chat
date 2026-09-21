from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Chunk, SourceType
from maple_chat.knowledge.audit import audit_knowledge_terms
from maple_chat.knowledge.service import sync_builtin_knowledge

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_corpus_audit_reports_differences_without_mutating_canonical_graph(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    texts = (
        "놀긍(놀러긍)이라고 잘못 설명했다.",
        "놀긍(놀라운 긍정의 혼돈 주문서)이 정확한 원래 이름이다.",
        "놀긍떡작 이후 프악공작을 진행했다.",
    )
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)
        for index, text in enumerate(texts):
            session.add(
                Chunk(
                    chunk_id=f"{index + 1:064d}",
                    source_type=SourceType.ARTICLE,
                    source_key=f"article:2304:{index + 1}",
                    ordinal=0,
                    text=text,
                    token_count=10,
                    metadata_json={},
                    chunking_revision="audit-test",
                    active=True,
                )
            )

    async with factory() as session:
        result = await audit_knowledge_terms(session, checked_at=now)

    assert result.active_chunks == 3
    assert result.audited_terms == 13
    assert result.different_expansion_candidates == 1
    term = next(term for term in result.terms if term.abbreviation == "놀긍")
    assert term.abbreviation_chunks == 3
    assert term.official_name_chunks == 1
    assert term.matching_expansions == 1
    assert [candidate.candidate for candidate in term.different_expansions] == ["놀러긍"]
    assert term.observed_compounds[0].surface == "놀긍떡작"
