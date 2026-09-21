"""Durable seven-board sync/run planning with overlap suppression."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.config import Settings
from maple_chat.crawler.policy import CrawlScope, require_live_crawl
from maple_chat.db.models import Board, CrawlRun, RunState
from maple_chat.jobs.queue import enqueue_job
from maple_chat.time import (
    KST,
    next_kst_occurrence,
    next_kst_weekday_occurrence,
    require_aware_utc,
)

BOARD_NAMES: dict[int, str] = {
    2294: "전사",
    2295: "마법사",
    2296: "궁수",
    2297: "도적",
    2298: "해적",
    2300: "질문과 답변",
    2304: "팁과 노하우",
}


class BackfillMode(StrEnum):
    FIXTURE = "fixture"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class PlannedRun:
    run_id: int
    created: bool
    jobs: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BackfillSlice:
    board_id: int
    start_page: int
    max_pages: int


def plan_round_robin_backfill(
    next_pages: Mapping[int, int | None],
    *,
    target_page: int = 200,
    page_batch: int = 10,
) -> tuple[BackfillSlice, ...]:
    """Plan resumable board slices in page rounds without crossing the target page."""
    if target_page < 1 or page_batch < 1 or page_batch > target_page:
        raise ValueError("backfill target and page batch are invalid")

    remaining: dict[int, int | None] = {}
    for board_id in BOARD_NAMES:
        next_page = next_pages.get(board_id, 1)
        if next_page is not None and next_page < 1:
            raise ValueError("backfill checkpoint page must be positive")
        remaining[board_id] = next_page

    plan: list[BackfillSlice] = []
    for round_start in range(1, target_page + 1, page_batch):
        round_end = min(target_page, round_start + page_batch - 1)
        for board_id in BOARD_NAMES:
            next_page = remaining[board_id]
            if next_page is None or next_page > target_page or next_page > round_end:
                continue
            max_pages = min(page_batch, round_end - next_page + 1)
            plan.append(BackfillSlice(board_id, next_page, max_pages))
            remaining[board_id] = next_page + max_pages
    return tuple(plan)


def parse_daily_time(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("CRAWL_DAILY_AT must use HH:MM format") from exc
    if parsed.tzinfo is not None:
        raise ValueError("CRAWL_DAILY_AT must be a local KST wall-clock time")
    return parsed


def next_daily_run(value: str, now: datetime) -> datetime:
    return next_kst_occurrence(parse_daily_time(value), now)


def next_weekly_patch_run(value: str, now: datetime) -> datetime:
    """Schedule the official patch-note refresh for Thursday (weekday 3) in KST."""

    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("PATCH_NOTES_WEEKLY_AT must use HH:MM format") from exc
    if parsed.tzinfo is not None:
        raise ValueError("PATCH_NOTES_WEEKLY_AT must be a local KST wall-clock time")
    return next_kst_weekday_occurrence(parsed, 3, now)


async def seed_boards(session: AsyncSession) -> None:
    for board_id, name in BOARD_NAMES.items():
        await session.execute(
            insert(Board)
            .values(board_id=board_id, name=name, crawl_checkpoint={})
            .on_conflict_do_update(
                index_elements=[Board.board_id],
                set_={"name": name},
            )
        )


async def _active_run(session: AsyncSession, run_type: str) -> CrawlRun | None:
    run: CrawlRun | None = await session.scalar(
        sa.select(CrawlRun)
        .where(
            CrawlRun.run_type == run_type,
            CrawlRun.state.in_([RunState.QUEUED, RunState.RUNNING]),
        )
        .with_for_update()
    )
    return run


async def plan_daily_incremental(session: AsyncSession, *, now: datetime) -> PlannedRun:
    scheduled_at = require_aware_utc(now)
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext("daily-sync"))))
    existing = await _active_run(session, "daily_incremental")
    if existing is not None:
        return PlannedRun(existing.id, False, ())
    run = CrawlRun(
        run_type="daily_incremental",
        scope={
            "board_ids": sorted(BOARD_NAMES),
            "scheduled_kst": scheduled_at.astimezone(KST).isoformat(),
        },
        state=RunState.QUEUED,
        counters={"planned": len(BOARD_NAMES), "succeeded": 0, "failed": 0},
        created_at=scheduled_at,
    )
    session.add(run)
    await session.flush()
    jobs = tuple(
        [
            await enqueue_job(
                session,
                job_type="sync_board",
                dedupe_key=f"daily:{scheduled_at.astimezone(KST).date()}:{board_id}",
                payload={"run_id": run.id, "board_id": board_id, "page": 1, "mode": "incremental"},
                priority=100,
            )
            for board_id in sorted(BOARD_NAMES)
        ]
    )
    return PlannedRun(run.id, True, jobs)


async def plan_recent_first(session: AsyncSession, *, now: datetime) -> PlannedRun:
    planned_at = require_aware_utc(now)
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext("recent-sync"))))
    existing = await _active_run(session, "recent_first")
    if existing is not None:
        return PlannedRun(existing.id, False, ())
    run = CrawlRun(
        run_type="recent_first",
        scope={"board_ids": sorted(BOARD_NAMES), "page": 1},
        state=RunState.QUEUED,
        counters={"planned": len(BOARD_NAMES), "succeeded": 0, "failed": 0},
        created_at=planned_at,
    )
    session.add(run)
    await session.flush()
    jobs: list[int] = []
    for board_id in sorted(BOARD_NAMES):
        jobs.append(
            await enqueue_job(
                session,
                job_type="crawl_listing",
                dedupe_key=f"recent:{board_id}:page:1",
                payload={"run_id": run.id, "board_id": board_id, "page": 1, "mode": "recent"},
                priority=200,
            )
        )
    return PlannedRun(run.id, True, tuple(jobs))


async def plan_backfill_page(
    session: AsyncSession,
    *,
    settings: Settings,
    board_id: int,
    mode: BackfillMode,
) -> int | None:
    if board_id not in BOARD_NAMES:
        raise ValueError("board is outside the approved seven-board scope")
    if mode is BackfillMode.LIVE:
        require_live_crawl(
            settings,
            f"https://www.inven.co.kr/board/maple/{board_id}",
            CrawlScope.FULL_BACKFILL,
        )
    board = await session.get(Board, board_id, with_for_update=True)
    if board is None:
        raise LookupError("board is not seeded")
    checkpoint = dict(board.crawl_checkpoint)
    if checkpoint.get("backfill_complete") is True:
        return None
    page = int(checkpoint.get("next_backfill_page", 2))
    return await enqueue_job(
        session,
        job_type="crawl_listing",
        dedupe_key=f"backfill:{mode.value}:{board_id}:page:{page}",
        payload={"board_id": board_id, "page": page, "mode": mode.value},
        priority=10,
    )


async def record_board_progress(
    session: AsyncSession,
    *,
    board_id: int,
    page: int,
    observed_article_ids: tuple[int, ...],
    completed: bool,
    succeeded_at: datetime,
) -> None:
    board = await session.get(Board, board_id, with_for_update=True)
    if board is None:
        raise LookupError("board is not seeded")
    checkpoint = dict(board.crawl_checkpoint)
    checkpoint.update(
        {
            "last_successful_page": page,
            "next_backfill_page": page + 1,
            "earliest_article_id": min(observed_article_ids)
            if observed_article_ids
            else checkpoint.get("earliest_article_id"),
            "backfill_complete": completed,
            "recent_ready": True,
            "last_success_at": require_aware_utc(succeeded_at).isoformat(),
        }
    )
    board.crawl_checkpoint = checkpoint
    board.updated_at = require_aware_utc(succeeded_at)
    await session.flush()


async def record_board_refresh(
    session: AsyncSession,
    *,
    board_id: int,
    succeeded_at: datetime,
) -> None:
    """Record a latest-page refresh without changing historical backfill position."""
    board = await session.get(Board, board_id, with_for_update=True)
    if board is None:
        raise LookupError("board is not seeded")
    refreshed_at = require_aware_utc(succeeded_at)
    checkpoint = dict(board.crawl_checkpoint)
    checkpoint.update(
        {
            "recent_ready": True,
            "last_refresh_at": refreshed_at.isoformat(),
        }
    )
    board.crawl_checkpoint = checkpoint
    board.updated_at = refreshed_at
    await session.flush()


async def finish_run(
    session: AsyncSession,
    *,
    run_id: int,
    succeeded: int,
    failed: int,
    finished_at: datetime,
) -> RunState:
    run = await session.get(CrawlRun, run_id, with_for_update=True)
    if run is None:
        raise LookupError("crawl run not found")
    run.counters = {**run.counters, "succeeded": succeeded, "failed": failed}
    run.finished_at = require_aware_utc(finished_at)
    if failed and succeeded:
        run.state = RunState.PARTIAL
    elif failed:
        run.state = RunState.FAILED
    else:
        run.state = RunState.SUCCEEDED
    await session.flush()
    return run.state
