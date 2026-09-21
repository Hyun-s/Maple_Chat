from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.config import ProcessRole, Settings
from maple_chat.crawler.fixture import FixtureCorpus, ingest_fixture_board
from maple_chat.crawler.policy import LiveCrawlDenied
from maple_chat.db.models import (
    Article,
    Board,
    Category,
    Comment,
    CrawlJob,
    OutboxEvent,
    RunState,
)
from maple_chat.scheduler.planner import (
    BOARD_NAMES,
    BackfillMode,
    finish_run,
    plan_backfill_page,
    plan_daily_incremental,
    plan_recent_first,
    record_board_progress,
    seed_boards,
)

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


def fixture_settings() -> Settings:
    return Settings(
        role=ProcessRole.SCHEDULER,
        database_url="postgresql+asyncpg://fixture@localhost/maple_chat",
        live_crawl_enabled=False,
    )


@pytest.mark.asyncio
async def test_seven_board_runs_suppress_overlap_and_preserve_partial_failure(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    async with factory() as session, session.begin():
        await seed_boards(session)
        recent = await plan_recent_first(session, now=now)
    assert recent.created is True
    assert len(recent.jobs) == 7

    async with factory() as session, session.begin():
        duplicate = await plan_recent_first(session, now=now)
        assert duplicate == type(duplicate)(recent.run_id, False, ())
        assert (
            await finish_run(
                session,
                run_id=recent.run_id,
                succeeded=6,
                failed=1,
                finished_at=now,
            )
            == RunState.PARTIAL
        )
        daily = await plan_daily_incremental(session, now=now)
    assert daily.created is True
    assert len(daily.jobs) == 7

    async with factory() as session, session.begin():
        overlap = await plan_daily_incremental(session, now=now)
    assert overlap.created is False
    assert overlap.run_id == daily.run_id
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Board)) == 7
        assert await session.scalar(sa.select(sa.func.count()).select_from(CrawlJob)) == 14


@pytest.mark.asyncio
async def test_backfill_checkpoint_resumes_and_live_mode_stays_rights_gated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    settings = fixture_settings()
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
    async with factory() as session, session.begin():
        first_job = await plan_backfill_page(
            session,
            settings=settings,
            board_id=2304,
            mode=BackfillMode.FIXTURE,
        )
    async with factory() as session, session.begin():
        resumed_job = await plan_backfill_page(
            session,
            settings=settings,
            board_id=2304,
            mode=BackfillMode.FIXTURE,
        )
        with pytest.raises(LiveCrawlDenied, match="LIVE_CRAWL_ENABLED=false"):
            await plan_backfill_page(
                session,
                settings=settings,
                board_id=2304,
                mode=BackfillMode.LIVE,
            )
    assert first_job == resumed_job
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
