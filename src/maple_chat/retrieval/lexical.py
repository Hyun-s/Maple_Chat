"""Two-stage lexical retrieval with GIN tsvector pre-filtering and trigram similarity."""

from __future__ import annotations

import re
from collections.abc import Sequence
from time import perf_counter
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Chunk

if TYPE_CHECKING:
    from maple_chat.retrieval.hybrid import Candidate

# Parallel scan worker tuning for the fallback full scan path.
# Dropping the table scan cap to 1MB plans 4 workers on PostgreSQL.
_LEXICAL_SCAN_GUC_STATEMENTS: tuple[str, ...] = (
    "SET LOCAL min_parallel_table_scan_size = '1MB'",
    "SET LOCAL max_parallel_workers_per_gather = 4",
)

_DEFAULT_STAGE1_LIMIT: int = 600
_KEYWORD_PATTERN = re.compile(r"[a-zA-Z0-9가-힣]+")


def extract_keywords(query: str, max_keywords: int = 10) -> list[str]:
    """Extract clean alphanumeric and Korean tokens from query string."""
    tokens = _KEYWORD_PATTERN.findall(query)
    return [t for t in tokens if 0 < len(t) <= 100][:max_keywords]


def build_tsquery(query: str) -> str | None:
    """Build a tsquery expression string using prefix matching (:*) for terms."""
    keywords = extract_keywords(query)
    if not keywords:
        return None
    escaped = [k.replace("'", "''") for k in keywords]
    return " | ".join(f"'{k}':*" for k in escaped)


async def _execute_two_stage(
    session: AsyncSession,
    query: str,
    tsquery_expr: str,
    limit: int,
    stage1_limit: int,
) -> Sequence[Any]:
    stage1 = (
        sa.select(
            Chunk.chunk_id,
            Chunk.source_key,
            Chunk.text,
            Chunk.metadata_json,
        )
        .where(
            Chunk.active.is_(True),
            sa.func.to_tsvector("simple", Chunk.text).op("@@")(
                sa.func.to_tsquery("simple", tsquery_expr)
            ),
        )
        .limit(stage1_limit)
        .cte("stage1")
    )
    similarity = sa.func.similarity(stage1.c.text, query).label("similarity")
    stmt = (
        sa.select(
            stage1.c.chunk_id,
            stage1.c.source_key,
            stage1.c.text,
            stage1.c.metadata_json,
            similarity,
        )
        .where(similarity > 0)
        .order_by(similarity.desc(), stage1.c.chunk_id)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.all()


async def _execute_parallel_scan_fallback(
    session: AsyncSession,
    query: str,
    limit: int,
) -> Sequence[Any]:
    for statement in _LEXICAL_SCAN_GUC_STATEMENTS:
        await session.execute(sa.text(statement))
    similarity = sa.func.similarity(Chunk.text, query).label("similarity")
    result = await session.execute(
        sa.select(Chunk, similarity)
        .where(Chunk.active.is_(True), similarity > 0)
        .order_by(similarity.desc(), Chunk.chunk_id)
        .limit(limit)
    )
    return result.all()


async def lexical_candidates(
    factory: async_sessionmaker[AsyncSession],
    query: str,
    limit: int,
    *,
    stage1_limit: int = _DEFAULT_STAGE1_LIMIT,
) -> tuple[list[Candidate], float]:
    """Retrieve lexical candidates via two-stage GIN pre-filtering with similarity ranking."""
    from maple_chat.retrieval.hybrid import Candidate

    tsquery_expr = build_tsquery(query)
    query_started_at = perf_counter()

    async with factory() as session, session.begin():
        if tsquery_expr is not None:
            two_stage_rows = await _execute_two_stage(
                session, query, tsquery_expr, limit, stage1_limit
            )
            if two_stage_rows:
                query_seconds = perf_counter() - query_started_at
                return (
                    [
                        Candidate(
                            chunk_id=chunk_id,
                            source_key=source_key,
                            text=text,
                            metadata=metadata,
                            lexical_score=float(score),
                        )
                        for chunk_id, source_key, text, metadata, score in two_stage_rows
                    ],
                    query_seconds,
                )

        # Fallback to parallel seq scan if tsquery was empty or two-stage yielded no matches
        fallback_rows = await _execute_parallel_scan_fallback(session, query, limit)
        query_seconds = perf_counter() - query_started_at

    return (
        [
            Candidate(
                chunk_id=chunk.chunk_id,
                source_key=chunk.source_key,
                text=chunk.text,
                metadata=chunk.metadata_json,
                lexical_score=float(value),
            )
            for chunk, value in fallback_rows
        ],
        query_seconds,
    )
