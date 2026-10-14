from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.crawler.fixture import FixtureCorpus, ingest_fixture_board
from maple_chat.db.models import Article, Board, Category, Comment, OutboxEvent
from maple_chat.scheduler.planner import (
    BOARD_NAMES,
    plan_round_robin_backfill,
    record_board_progress,
    seed_boards,
)

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_backfill_checkpoint_resumes_without_crossing_the_target_page(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    async with factory() as session, session.begin():
        await seed_boards(session)
        await record_board_progress(
            session,
            board_id=2304,
            page=2,
            observed_article_ids=(30, 20),
            completed=False,
            succeeded_at=now,
        )
    async with factory() as session:
        rows = (
            await session.execute(
                sa.select(Board.board_id, Board.crawl_checkpoint).where(
                    Board.board_id.in_(sorted(BOARD_NAMES))
                )
            )
        ).all()
    checkpoints = {
        board_id: None
        if checkpoint.get("backfill_complete") is True
        else int(checkpoint.get("next_backfill_page", 1))
        for board_id, checkpoint in rows
    }
    plan = plan_round_robin_backfill(checkpoints, target_page=200, page_batch=10)
    resumed = [item for item in plan if item.board_id == 2304]
    assert resumed[0].start_page == 3
    assert all(item.start_page + item.max_pages - 1 <= 200 for item in plan)
    async with factory() as session:
        board = await session.get(Board, 2304)
        assert board is not None
        assert board.crawl_checkpoint["next_backfill_page"] == 3
        assert board.crawl_checkpoint["earliest_article_id"] == 20


@pytest.mark.asyncio
async def test_all_seven_fixture_boards_ingest_idempotently_without_raw_nicknames(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    corpus = FixtureCorpus(Path("tests/fixtures/inven"))
    async with factory() as session, session.begin():
        await seed_boards(session)
        first = [
            await ingest_fixture_board(
                session,
                corpus=corpus,
                board_id=board_id,
                observed_at=now,
                nickname_salt="x" * 32,
            )
            for board_id in sorted(BOARD_NAMES)
        ]
    assert [item.board_id for item in first] == sorted(BOARD_NAMES)
    assert sum(item.articles for item in first) == 7
    assert sum(item.comments for item in first) == 3

    async with factory() as session, session.begin():
        for board_id in sorted(BOARD_NAMES):
            await ingest_fixture_board(
                session,
                corpus=corpus,
                board_id=board_id,
                observed_at=now,
                nickname_salt="x" * 32,
            )
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Article)) == 7
        assert await session.scalar(sa.select(sa.func.count()).select_from(Category)) == 14
        assert await session.scalar(sa.select(sa.func.count()).select_from(Comment)) == 3
        assert await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 10
        author_hashes = list(await session.scalars(sa.select(Article.author_hash)))
        assert all(value is not None and "fixture" not in value for value in author_hashes)
