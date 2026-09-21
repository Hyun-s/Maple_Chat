#!/usr/bin/env python3
"""Read-only validation for the production recovery workflow."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import sqlalchemy as sa

from maple_chat.config import ProcessRole, load_settings
from maple_chat.db.models import (
    Article,
    Board,
    Chunk,
    ChunkEmbedding,
    Comment,
    EmbeddingRevision,
    KnowledgeAlias,
    KnowledgeEntity,
    KnowledgeRelation,
    KnowledgeSource,
    SourceStatus,
)
from maple_chat.db.session import create_engine, create_session_factory

PATCH_NOTE_BOARD_ID = -1001
EXPECTED_GRAPH_COUNTS = {
    "knowledge_sources": 7,
    "knowledge_entities": 217,
    "knowledge_aliases": 755,
    "knowledge_relations": 292,
}
TARGET_PAGES = {
    2294: 200,
    2295: 200,
    2296: 200,
    2297: 200,
    2298: 200,
    2300: 200,
    2304: 200,
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    report: dict[str, object]
    problems: tuple[str, ...]


async def validate() -> CheckResult:
    engine = create_engine(load_settings(ProcessRole.SCHEDULER))
    factory = create_session_factory(engine)
    problems: list[str] = []
    try:
        async with factory() as session:
            articles = int(await session.scalar(sa.select(sa.func.count()).select_from(Article)))
            indexed_articles = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Article)
                    .where(Article.status == SourceStatus.INDEXED)
                )
            )
            pending_articles = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Article)
                    .where(Article.status == SourceStatus.SANITIZED)
                )
            )
            comments = int(await session.scalar(sa.select(sa.func.count()).select_from(Comment)))
            indexed_comments = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Comment)
                    .where(Comment.status == SourceStatus.INDEXED)
                )
            )
            pending_comments = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Comment)
                    .where(Comment.status == SourceStatus.SANITIZED)
                )
            )
            active_chunks = int(
                await session.scalar(
                    sa.select(sa.func.count()).select_from(Chunk).where(Chunk.active.is_(True))
                )
            )
            active_embeddings = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(ChunkEmbedding)
                    .where(ChunkEmbedding.active.is_(True))
                )
            )
            active_revisions = (
                await session.execute(
                    sa.select(
                        EmbeddingRevision.name,
                        EmbeddingRevision.dimension,
                    ).where(EmbeddingRevision.active.is_(True))
                )
            ).all()
            patch_notes = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Article)
                    .where(
                        Article.board_id == PATCH_NOTE_BOARD_ID,
                        Article.status == SourceStatus.INDEXED,
                    )
                )
            )
            graph_counts = {
                "knowledge_sources": int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(KnowledgeSource)
                        .where(KnowledgeSource.active.is_(True))
                    )
                ),
                "knowledge_entities": int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(KnowledgeEntity)
                        .where(KnowledgeEntity.active.is_(True))
                    )
                ),
                "knowledge_aliases": int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(KnowledgeAlias)
                        .where(KnowledgeAlias.active.is_(True))
                    )
                ),
                "knowledge_relations": int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(KnowledgeRelation)
                        .where(KnowledgeRelation.active.is_(True))
                    )
                ),
            }
            boards = (
                await session.execute(
                    sa.select(Board.board_id, Board.name, Board.crawl_checkpoint).where(
                        Board.board_id.in_(TARGET_PAGES)
                    )
                )
            ).all()
            fixture_chunks = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Chunk)
                    .where(
                        Chunk.chunk_id == "c" * 64,
                        Chunk.source_key == "article:2304:1",
                        Chunk.text == "정답 근거 본문",
                    )
                )
            )

        board_report: dict[str, object] = {}
        seen_boards: set[int] = set()
        for board_id, name, checkpoint in boards:
            seen_boards.add(board_id)
            target = TARGET_PAGES[board_id]
            last_page = int(checkpoint.get("last_successful_page", 0))
            complete = checkpoint.get("backfill_complete") is True
            board_report[str(board_id)] = {
                "name": name,
                "target_page": target,
                "last_successful_page": last_page,
                "next_backfill_page": checkpoint.get("next_backfill_page"),
                "source_exhausted": complete,
            }
            if not complete and last_page < target:
                problems.append(f"board {board_id} stopped at page {last_page}, target {target}")
        for board_id in TARGET_PAGES.keys() - seen_boards:
            problems.append(f"board {board_id} is missing")

        for name, expected in EXPECTED_GRAPH_COUNTS.items():
            actual = graph_counts[name]
            if actual != expected:
                problems.append(f"{name}={actual}, expected {expected}")
        if pending_articles:
            problems.append(f"{pending_articles} sanitized articles remain unindexed")
        if pending_comments:
            problems.append(f"{pending_comments} sanitized comments remain unindexed")
        if active_chunks < 1:
            problems.append("no active RAG chunks were generated")
        if active_chunks != active_embeddings:
            problems.append(
                f"active chunk/embedding mismatch: {active_chunks} != {active_embeddings}"
            )
        if len(active_revisions) != 1:
            problems.append(
                f"active embedding revision count is {len(active_revisions)}, expected 1"
            )
        elif active_revisions[0].dimension != 1024:
            problems.append(
                f"active embedding dimension is {active_revisions[0].dimension}, expected 1024"
            )
        if patch_notes < 1:
            problems.append("no indexed official patch notes were recovered")
        if fixture_chunks:
            problems.append("the known integration-test chunk fixture is still present")

        report: dict[str, object] = {
            "status": "passed" if not problems else "failed",
            "coverage_note": (
                "Page coverage is restored from current live sources; byte-identical historical "
                "rows and deleted/edited posts cannot be guaranteed."
            ),
            "corpus": {
                "articles": articles,
                "indexed_articles": indexed_articles,
                "comments": comments,
                "indexed_comments": indexed_comments,
                "active_chunks": active_chunks,
                "active_embeddings": active_embeddings,
                "official_patch_notes": patch_notes,
            },
            "active_embedding_revisions": [
                {"name": row.name, "dimension": row.dimension} for row in active_revisions
            ],
            "knowledge_graph": graph_counts,
            "boards": board_report,
            "problems": problems,
        }
        return CheckResult(report, tuple(problems))
    finally:
        await engine.dispose()


def main() -> int:
    result = asyncio.run(validate())
    print(json.dumps(result.report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
