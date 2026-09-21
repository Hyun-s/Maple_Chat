from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Chunk, EmbeddingRevision, SourceType
from maple_chat.db.repositories import activate_embedding_revision
from maple_chat.indexing.chunking import ChunkDraft, SourceDocument, chunk_document
from maple_chat.indexing.embedding import EmbeddingSpec, index_chunk_batch
from maple_chat.retrieval.hybrid import hybrid_retrieve

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


class FixedEmbedding:
    def __init__(self, spec: EmbeddingSpec, *, reverse: bool = False) -> None:
        self.spec = spec
        self.reverse = reverse

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            answer = "정답 근거" in text
            first = (not answer) if self.reverse else answer
            vectors.append(([1.0, 0.0] if first else [0.0, 1.0]) + [0.0] * 1022)
        return vectors


class ConstantReranker:
    async def score(self, query: str, documents: list[str]) -> list[float]:
        return [0.5] * len(documents)


def draft(source_key: str, text: str) -> tuple[ChunkDraft, ...]:
    return chunk_document(
        SourceDocument(
            source_type=SourceType.ARTICLE,
            source_key=source_key,
            title=text,
            board="팁과 노하우",
            category="사냥",
            published_at=datetime(2026, 8, 4, tzinfo=UTC),
            text=text,
            metadata={},
        )
    )


@pytest.mark.asyncio
async def test_idempotent_vectors_revision_cutover_and_active_filter(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    first_spec = EmbeddingSpec("bge-v1", "BAAI/bge-m3", "commit-1")
    second_spec = EmbeddingSpec("bge-v2", "BAAI/bge-m3", "commit-2")
    drafts = (*draft("article:2304:1", "정답 근거"), *draft("article:2304:2", "다른 자료"))
    async with factory() as session, session.begin():
        first = EmbeddingRevision(
            name=first_spec.name,
            model=first_spec.model,
            model_commit=first_spec.model_commit,
            dimension=first_spec.dimension,
            normalized=first_spec.normalized,
            distance=first_spec.distance,
        )
        session.add(first)
        await session.flush()
        assert (
            await index_chunk_batch(
                session,
                drafts=drafts,
                revision=first,
                provider=FixedEmbedding(first_spec),
            )
            == 2
        )
        assert (
            await index_chunk_batch(
                session,
                drafts=drafts,
                revision=first,
                provider=FixedEmbedding(first_spec),
            )
            == 0
        )
        await activate_embedding_revision(session, first.id, activated_at=now)

    query_vector = [1.0, 0.0] + [0.0] * 1022
    result = await hybrid_retrieve(
        factory,
        query="lexically unrelated qzxw",
        query_vector=query_vector,
        reranker=ConstantReranker(),
        now=now,
    )
    assert result.insufficient_evidence is False
    assert result.evidence[0].source_key == "article:2304:1"

    async with factory() as session, session.begin():
        second = EmbeddingRevision(
            name=second_spec.name,
            model=second_spec.model,
            model_commit=second_spec.model_commit,
            dimension=second_spec.dimension,
            normalized=second_spec.normalized,
            distance=second_spec.distance,
        )
        session.add(second)
        await session.flush()
        await index_chunk_batch(
            session,
            drafts=drafts,
            revision=second,
            provider=FixedEmbedding(second_spec, reverse=True),
        )
        await activate_embedding_revision(session, second.id, activated_at=now)

    cutover = await hybrid_retrieve(
        factory,
        query="lexically unrelated qzxw",
        query_vector=query_vector,
        reranker=ConstantReranker(),
        now=now,
    )
    assert cutover.evidence[0].source_key == "article:2304:2"

    async with factory() as session, session.begin():
        await session.execute(
            sa.update(Chunk).where(Chunk.source_key == "article:2304:2").values(active=False)
        )
    filtered = await hybrid_retrieve(
        factory,
        query="lexically unrelated qzxw",
        query_vector=query_vector,
        reranker=ConstantReranker(),
        now=now,
    )
    assert [candidate.source_key for candidate in filtered.evidence] == ["article:2304:1"]


@pytest.mark.asyncio
async def test_low_rerank_score_refuses_generation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    spec = EmbeddingSpec("bge-v1", "BAAI/bge-m3", "commit-1")
    async with factory() as session, session.begin():
        revision = EmbeddingRevision(
            name=spec.name,
            model=spec.model,
            model_commit=spec.model_commit,
            dimension=spec.dimension,
            normalized=spec.normalized,
            distance=spec.distance,
        )
        session.add(revision)
        await session.flush()
        await index_chunk_batch(
            session,
            drafts=draft("article:2304:1", "정답 근거"),
            revision=revision,
            provider=FixedEmbedding(spec),
        )
        await activate_embedding_revision(session, revision.id, activated_at=now)
    result = await hybrid_retrieve(
        factory,
        query="query",
        query_vector=[1.0, 0.0] + [0.0] * 1022,
        reranker=ConstantReranker(),
        now=now,
        minimum_score=0.9,
    )
    assert result.insufficient_evidence is True
    assert result.evidence == ()
