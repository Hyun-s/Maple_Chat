"""Lease-based, idempotent PostgreSQL queue using SKIP LOCKED."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import CrawlJob, JobState
from maple_chat.time import require_aware_utc, utc_now


@dataclass(frozen=True, slots=True)
class JobLease:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempts: int
    leased_until: datetime


def retry_delay(attempt: int, base_seconds: int = 5, cap_seconds: int = 3600) -> timedelta:
    if attempt < 1:
        raise ValueError("attempt must be positive")
    return timedelta(seconds=min(cap_seconds, base_seconds * (2 ** (attempt - 1))))


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: str,
    dedupe_key: str,
    payload: dict[str, Any],
    priority: int = 0,
    max_attempts: int = 5,
) -> int:
    statement = (
        insert(CrawlJob)
        .values(
            job_type=job_type,
            dedupe_key=dedupe_key,
            payload=payload,
            state=JobState.QUEUED,
            priority=priority,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=[CrawlJob.job_type, CrawlJob.dedupe_key],
            index_where=sa.text("state IN ('queued', 'leased')"),
        )
        .returning(CrawlJob.id)
    )
    inserted = await session.scalar(statement)
    if inserted is not None:
        return inserted
    existing = await session.scalar(
        sa.select(CrawlJob.id).where(
            CrawlJob.job_type == job_type,
            CrawlJob.dedupe_key == dedupe_key,
            CrawlJob.state.in_([JobState.QUEUED, JobState.LEASED]),
        )
    )
    if existing is None:
        raise RuntimeError("active deduplicated job disappeared")
    return existing


async def lease_jobs(
    factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    limit: int = 1,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> list[JobLease]:
    leased_at = require_aware_utc(now or utc_now())
    lease_until = leased_at + timedelta(seconds=lease_seconds)
    async with factory() as session, session.begin():
        jobs = list(
            await session.scalars(
                sa.select(CrawlJob)
                .where(
                    CrawlJob.state == JobState.QUEUED,
                    CrawlJob.next_attempt_at <= leased_at,
                )
                .order_by(CrawlJob.priority.desc(), CrawlJob.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        leases: list[JobLease] = []
        for job in jobs:
            job.state = JobState.LEASED
            job.lease_owner = worker_id
            job.leased_until = lease_until
            job.attempts += 1
            job.updated_at = leased_at
            leases.append(
                JobLease(
                    id=job.id,
                    job_type=job.job_type,
                    payload=job.payload,
                    attempts=job.attempts,
                    leased_until=lease_until,
                )
            )
        return leases


async def acknowledge_job(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    completed_at = require_aware_utc(now or utc_now())
    result = cast(
        CursorResult[Any],
        await session.execute(
            sa.update(CrawlJob)
            .where(
                CrawlJob.id == job_id,
                CrawlJob.state == JobState.LEASED,
                CrawlJob.lease_owner == worker_id,
            )
            .values(
                state=JobState.SUCCEEDED,
                lease_owner=None,
                leased_until=None,
                updated_at=completed_at,
            )
        ),
    )
    return bool(result.rowcount)


async def fail_job(
    session: AsyncSession,
    *,
    job_id: int,
    worker_id: str,
    error_code: str,
    now: datetime | None = None,
) -> JobState:
    failed_at = require_aware_utc(now or utc_now())
    job = await session.scalar(
        sa.select(CrawlJob)
        .where(
            CrawlJob.id == job_id,
            CrawlJob.state == JobState.LEASED,
            CrawlJob.lease_owner == worker_id,
        )
        .with_for_update()
    )
    if job is None:
        raise RuntimeError("leased job ownership mismatch")
    dead = job.attempts >= job.max_attempts
    job.state = JobState.DEAD_LETTER if dead else JobState.QUEUED
    job.next_attempt_at = failed_at if dead else failed_at + retry_delay(job.attempts)
    job.lease_owner = None
    job.leased_until = None
    job.last_error_code = error_code
    job.updated_at = failed_at
    return job.state


async def reclaim_expired_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    reclaimed_at = require_aware_utc(now or utc_now())
    jobs = list(
        await session.scalars(
            sa.select(CrawlJob)
            .where(
                CrawlJob.state == JobState.LEASED,
                CrawlJob.leased_until < reclaimed_at,
            )
            .with_for_update(skip_locked=True)
        )
    )
    queued = 0
    dead = 0
    for job in jobs:
        is_dead = job.attempts >= job.max_attempts
        job.state = JobState.DEAD_LETTER if is_dead else JobState.QUEUED
        job.next_attempt_at = reclaimed_at
        job.lease_owner = None
        job.leased_until = None
        job.last_error_code = "lease_expired"
        job.updated_at = reclaimed_at
        if is_dead:
            dead += 1
        else:
            queued += 1
    return queued, dead
