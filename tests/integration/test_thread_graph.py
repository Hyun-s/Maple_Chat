from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Article, Board, Comment, SourceStatus
from maple_chat.knowledge.threads import attach_thread_context, load_thread_graph
from maple_chat.retrieval.hybrid import Candidate

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


async def seed(factory: async_sessionmaker[AsyncSession]) -> None:
    now = datetime(2026, 10, 2, tzinfo=UTC)
    async with factory() as session, session.begin():
        session.add(Board(board_id=2300, name="질문과 답변"))
        await session.flush()
        for article_id in (1, 2):
            session.add(
                Article(
                    id=article_id,
                    board_id=2300,
                    remote_article_id=article_id,
                    title=f"질문 {article_id}",
                    sanitized_body="질문 문맥",
                    url=f"https://www.inven.co.kr/board/maple/2300/{article_id}",
                    status=SourceStatus.INDEXED,
                    content_hash="a" * 64,
                    first_observed_at=now,
                    last_observed_at=now,
                )
            )
        await session.flush()
        for article_id, remote_id, parent, status in (
            (1, 10, None, SourceStatus.INDEXED),
            (1, 20, 10, SourceStatus.INDEXED),
            (1, 30, 20, SourceStatus.INDEXED),
            (1, 40, 30, SourceStatus.SANITIZED),
            (1, 50, None, SourceStatus.DELETED),
            (1, 60, 70, SourceStatus.INDEXED),
            (2, 70, None, SourceStatus.INDEXED),
        ):
            session.add(
                Comment(
                    article_id=article_id,
                    remote_comment_id=remote_id,
                    parent_remote_comment_id=parent,
                    sanitized_body=f"답변 {remote_id}",
                    status=status,
                    content_hash="c" * 64,
                    first_observed_at=now,
                    last_observed_at=now,
                )
            )


async def test_focused_graph_reads_ancestors_and_applies_depth_and_status_bounds(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory)
    async with factory() as session:
        graph = await load_thread_graph(
            session, board_id=2300, remote_article_id=1, remote_comment_id=30
        )
        assert graph is not None
        assert len(graph.nodes) == 4
        assert [e.predicate for e in graph.edges].count("reply_to") == 2
        assert not graph.incomplete
        bounded = await load_thread_graph(
            session, board_id=2300, remote_article_id=1, remote_comment_id=30, max_hops=1
        )
        assert bounded is not None
        assert bounded.incomplete
        assert len(bounded.nodes) == 3
        pending = await load_thread_graph(
            session,
            board_id=2300,
            remote_article_id=1,
            remote_comment_id=40,
            indexed_only=True,
        )
        assert pending is None
        cross_thread = await load_thread_graph(
            session, board_id=2300, remote_article_id=1, remote_comment_id=60
        )
        assert cross_thread is not None
        assert cross_thread.incomplete
        assert len(cross_thread.nodes) == 2
        assert all(e.predicate != "reply_to" for e in cross_thread.edges)


async def test_graph_updates_without_resync_and_does_not_include_deleted_rows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory)
    async with factory() as session:
        graph = await load_thread_graph(session, board_id=2300, remote_article_id=1, max_comments=2)
        assert graph is not None
        assert graph.incomplete
        assert len(graph.nodes) == 3
    async with factory() as session, session.begin():
        await session.execute(
            sa.update(Comment)
            .where(Comment.remote_comment_id == 20)
            .values(status=SourceStatus.DELETED)
        )
    async with factory() as session:
        graph = await load_thread_graph(
            session, board_id=2300, remote_article_id=1, remote_comment_id=30
        )
        assert graph is not None
        assert graph.incomplete
        assert len(graph.nodes) == 2
        assert all(n.source_key != "comment:article:2300:1:20" for n in graph.nodes)


async def test_retrieved_comment_gets_graph_without_changing_claim_or_rank(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed(factory)
    selected = Candidate(
        chunk_id="chunk",
        source_key="comment:article:2300:1:30",
        text="선택된 댓글",
        metadata={"qa_branch": True},
        rerank_score=0.9,
    )
    official = Candidate(
        chunk_id="official", source_key="article:-1001:1", text="공식 패치", metadata={}
    )
    result = await attach_thread_context(factory, (selected, official))
    assert result[0].text == selected.text
    assert result[0].rerank_score == selected.rerank_score
    assert result[0].metadata["qa_branch"] is True
    assert len(result[0].metadata["thread_graph"]["nodes"]) == 4
    assert "thread_graph" not in selected.metadata
    assert result[1] is official
