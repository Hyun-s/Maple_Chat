from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from maple_chat.time import KST, next_kst_occurrence, require_aware_utc, to_kst


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        require_aware_utc(datetime(2026, 8, 4))


def test_utc_and_kst_conversion_is_explicit() -> None:
    value = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    assert to_kst(value) == datetime(2026, 8, 4, 9, 0, tzinfo=KST)


def test_next_kst_occurrence_rolls_to_next_day() -> None:
    now = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
    assert next_kst_occurrence(time(4, 0), now) == datetime(2026, 8, 5, 19, 0, tzinfo=UTC)
