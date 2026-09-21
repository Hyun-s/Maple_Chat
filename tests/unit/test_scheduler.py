from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maple_chat.scheduler.planner import (
    BOARD_NAMES,
    next_daily_run,
    next_weekly_patch_run,
    parse_daily_time,
    plan_round_robin_backfill,
)


def test_exact_seven_board_scope_is_seed_contract() -> None:
    assert BOARD_NAMES == {
        2294: "전사",
        2295: "마법사",
        2296: "궁수",
        2297: "도적",
        2298: "해적",
        2300: "질문과 답변",
        2304: "팁과 노하우",
    }


def test_daily_schedule_uses_kst_and_rolls_forward() -> None:
    now = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
    assert next_daily_run("05:00", now) == datetime(2026, 8, 5, 20, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="HH:MM"):
        parse_daily_time("invalid")
    with pytest.raises(ValueError, match="wall-clock"):
        parse_daily_time("04:00+09:00")


def test_patch_note_schedule_is_thursday_0500_kst_and_rolls_weekly() -> None:
    wednesday = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    assert next_weekly_patch_run("05:00", wednesday) == datetime(2026, 8, 19, 20, 0, tzinfo=UTC)
    thursday_after = datetime(2026, 8, 19, 20, 1, tzinfo=UTC)
    assert next_weekly_patch_run("05:00", thursday_after) == datetime(
        2026, 8, 26, 20, 0, tzinfo=UTC
    )
    with pytest.raises(ValueError, match="PATCH_NOTES_WEEKLY_AT"):
        next_weekly_patch_run("invalid", wednesday)


def test_backfill_plan_defaults_to_two_hundred_pages_in_ten_page_rounds() -> None:
    plan = plan_round_robin_backfill(
        {board_id: 1 for board_id in BOARD_NAMES},
        page_batch=10,
    )

    assert [(item.board_id, item.start_page, item.max_pages) for item in plan[:7]] == [
        (board_id, 1, 10) for board_id in BOARD_NAMES
    ]
    assert [(item.board_id, item.start_page, item.max_pages) for item in plan[7:14]] == [
        (board_id, 11, 10) for board_id in BOARD_NAMES
    ]
    assert [(item.board_id, item.start_page, item.max_pages) for item in plan[-7:]] == [
        (board_id, 191, 10) for board_id in BOARD_NAMES
    ]


def test_backfill_plan_resumes_partial_boards_and_skips_completed_or_past_target() -> None:
    plan = plan_round_robin_backfill(
        {
            2294: 76,
            2295: None,
            2296: 56,
            2297: 71,
            2298: 73,
            2300: 72,
            2304: 1,
        },
        target_page=72,
        page_batch=10,
    )

    by_board = {
        board_id: [item for item in plan if item.board_id == board_id] for board_id in BOARD_NAMES
    }
    assert by_board[2294] == []
    assert by_board[2295] == []
    assert [(item.start_page, item.max_pages) for item in by_board[2296]] == [
        (56, 5),
        (61, 10),
        (71, 2),
    ]
    assert [(item.start_page, item.max_pages) for item in by_board[2297]] == [(71, 2)]
    assert by_board[2298] == []
    assert [(item.start_page, item.max_pages) for item in by_board[2300]] == [(72, 1)]
    assert [(item.start_page, item.max_pages) for item in by_board[2304]][-1] == (71, 2)
