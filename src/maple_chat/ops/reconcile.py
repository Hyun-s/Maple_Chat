"""Startup reconciliation for exclusions, leases, and embedding invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import Article, DenylistEntry, EmbeddingRevision
from maple_chat.db.repositories import exclude_article
from maple_chat.jobs.queue import reclaim_expired_leases


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    exclusions: int
    reclaimed_jobs: int
    dead_jobs: int
    active_embedding_revisions: int


async def reconcile_startup(session: AsyncSession, *, now: datetime) -> ReconcileResult:
    exclusions = 0
    entries = list(
        await session.scalars(sa.select(DenylistEntry).where(DenylistEntry.active.is_(True)))
    )
    for entry in entries:
        parts = entry.source_key.split(":")
        if len(parts) != 3 or parts[0] != "article":
            continue
        try:
            board_id, remote_article_id = int(parts[1]), int(parts[2])
        except ValueError:
            continue
        article = await session.scalar(
            sa.select(Article).where(
                Article.board_id == board_id,
                Article.remote_article_id == remote_article_id,
            )
        )
        if article is not None:
            await exclude_article(
                session,
                article=article,
                reason=entry.reason,
                observed_at=now,
                actor_hash=entry.actor_hash,
            )
            exclusions += 1
    reclaimed, dead = await reclaim_expired_leases(session, now=now)
    active_revisions = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(EmbeddingRevision)
            .where(EmbeddingRevision.active.is_(True))
        )
        or 0
    )
    if active_revisions > 1:
        raise RuntimeError("multiple active embedding revisions detected")
    return ReconcileResult(exclusions, reclaimed, dead, active_revisions)
