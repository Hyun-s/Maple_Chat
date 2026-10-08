"""Live DB/model smoke check; answer persistence is rolled back, Discord is not sent."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict

import sqlalchemy as sa

from maple_chat.config import ProcessRole, load_settings
from maple_chat.db.models import AnswerMode
from maple_chat.db.session import create_engine, create_session_factory
from maple_chat.knowledge.service import DatabaseKnowledgeRetriever
from maple_chat.knowledge.threads import load_thread_graph
from maple_chat.llm.client import LocalLLMClient
from maple_chat.qa.service import QAService, QuestionRequest
from maple_chat.runtime import DatabaseHybridRetriever, build_embedding_provider, build_reranker
from maple_chat.time import utc_now


async def main() -> None:
    settings = load_settings(ProcessRole.BOT)
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    embedding = build_embedding_provider(settings)
    reranker = build_reranker(settings)
    llm = LocalLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    try:
        started = time.perf_counter()
        async with factory() as session, session.begin():
            await session.execute(sa.text("SET TRANSACTION READ ONLY"))
            await session.execute(sa.text("SET LOCAL statement_timeout='30s'"))
            row = (
                await session.execute(
                    sa.text("""
                    SELECT a.board_id, a.remote_article_id, c.remote_comment_id
                    FROM comments c
                    JOIN articles a ON a.id=c.article_id
                    JOIN comments p ON p.article_id=c.article_id
                                   AND p.remote_comment_id=c.parent_remote_comment_id
                    WHERE c.status='indexed' AND p.status='indexed'
                      AND a.status='indexed' AND a.board_id>0
                      AND c.remote_comment_id<>p.remote_comment_id
                    LIMIT 1
                """)
                )
            ).one()
            graph = await load_thread_graph(
                session,
                board_id=row.board_id,
                remote_article_id=row.remote_article_id,
                remote_comment_id=row.remote_comment_id,
                indexed_only=True,
            )
        assert graph is not None
        assert any(edge.predicate == "reply_to" for edge in graph.edges)
        graph_seconds = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "stage": "live_graph",
                    "nodes": len(graph.nodes),
                    "edges": len(graph.edges),
                    "seconds": round(graph_seconds, 3),
                }
            ),
            flush=True,
        )
        knowledge = DatabaseKnowledgeRetriever(factory)
        retriever = DatabaseHybridRetriever(
            factory, embedding, reranker, knowledge_retriever=knowledge
        )
        service = QAService(retriever, llm, knowledge_retriever=knowledge)
        request = QuestionRequest(
            guild_id=int(settings.effective_discord_guild_ids[0]),
            channel_id=int(settings.discord_channel_ids[0]),
            request_message_id=-time.time_ns(),
            requester_hash="thread-graph-smoke",
            query=graph.title,
            received_at=utc_now(),
        )
        async with factory() as session:
            transaction = await session.begin()
            try:
                outcome = await service.answer(session, request)
                assert outcome.mode == AnswerMode.GENERATED, outcome.mode
                graph_sources = sum("thread_graph" in item.metadata for item in outcome.sources)
                assert graph_sources > 0, {
                    "query": request.query,
                    "sources": [item.source_key for item in outcome.sources],
                    "knowledge_facts": len(outcome.knowledge_facts),
                }
                assert outcome.text.strip()
                report = {
                    "stage": "live_rag_generation",
                    "query": request.query,
                    "mode": outcome.mode.value,
                    "evidence": len(outcome.sources),
                    "thread_graph_sources": graph_sources,
                    "knowledge_facts": len(outcome.knowledge_facts),
                    "answer": outcome.text,
                    "metrics": asdict(outcome.metrics) if outcome.metrics else None,
                }
            finally:
                await transaction.rollback()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
    finally:
        await embedding.aclose()
        await reranker.aclose()
        await llm.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
