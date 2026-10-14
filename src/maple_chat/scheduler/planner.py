"""Seven-board backfill planning and board checkpoint updates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import Board
from maple_chat.time import next_kst_weekday_occurrence, require_aware_utc

BOARD_NAMES: dict[int, str] = {
    2294: "전사",
    2295: "마법사",
    2296: "궁수",
    2297: "도적",
    2298: "해적",
    2300: "질문과 답변",
    2304: "팁과 노하우",
}


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
