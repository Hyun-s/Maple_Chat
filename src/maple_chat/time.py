"""UTC persistence and Asia/Seoul schedule conversions."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is forbidden")
    return value.astimezone(UTC)


def to_kst(value: datetime) -> datetime:
    return require_aware_utc(value).astimezone(KST)


def next_kst_occurrence(schedule: time, now: datetime | None = None) -> datetime:
    current = require_aware_utc(now or utc_now())
    current_kst = current.astimezone(KST)
    candidate = datetime.combine(current_kst.date(), schedule, tzinfo=KST)
    if candidate <= current_kst:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_kst_weekday_occurrence(
    schedule: time,
    weekday: int,
    now: datetime | None = None,
) -> datetime:
    """Return the next strictly-future KST weekday occurrence in UTC."""

    if weekday < 0 or weekday > 6:
        raise ValueError("weekday must be between Monday=0 and Sunday=6")
    current = require_aware_utc(now or utc_now())
    current_kst = current.astimezone(KST)
    days_ahead = (weekday - current_kst.weekday()) % 7
    candidate = datetime.combine(
        current_kst.date() + timedelta(days=days_ahead), schedule, tzinfo=KST
    )
    if candidate <= current_kst:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)
