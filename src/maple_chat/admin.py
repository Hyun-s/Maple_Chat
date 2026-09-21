"""Owner-only administration authorization and durable audit writes."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.crawler.sanitizer import hash_nickname
from maple_chat.db.models import (
    Article,
    AuditEvent,
    Board,
    CrawlJob,
    DenylistEntry,
    EmbeddingRevision,
    GuildSettings,
)
from maple_chat.db.repositories import article_source_key, exclude_article
from maple_chat.jobs.queue import enqueue_job
from maple_chat.time import utc_now


class AdminDenied(PermissionError):
    pass


class AdminService:
    def __init__(self, *, owner_id: int, pii_salt: str) -> None:
        self.owner_id = owner_id
        self.pii_salt = pii_salt

    def require_owner(self, actor_id: int) -> str:
        if actor_id != self.owner_id:
            raise AdminDenied("owner authorization required")
        return hash_nickname(str(actor_id), self.pii_salt)

    async def audit(
        self,
        session: AsyncSession,
        *,
        actor_id: int,
        action: str,
        target: str,
        result: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        actor_hash = self.require_owner(actor_id)
        session.add(
            AuditEvent(
                action=action,
                actor_hash=actor_hash,
                target=target,
                result=result,
                metadata_json=metadata or {},
            )
        )
        await session.flush()

    async def status(self, session: AsyncSession, *, actor_id: int) -> dict[str, Any]:
        self.require_owner(actor_id)
        queue_depth = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(CrawlJob)
                .where(CrawlJob.state.in_(["queued", "leased"]))
            )
            or 0
        )
        failed_jobs = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(CrawlJob)
                .where(CrawlJob.state == "dead_letter")
            )
            or 0
        )
        active_revision = await session.scalar(
            sa.select(EmbeddingRevision.name).where(EmbeddingRevision.active.is_(True))
        )
        boards = list(await session.scalars(sa.select(Board).order_by(Board.board_id)))
        return {
            "queue_depth": queue_depth,
            "failed_jobs": failed_jobs,
            "active_embedding_revision": active_revision,
            "boards": [
                {
                    "board_id": board.board_id,
                    "enabled": board.enabled,
                    "checkpoint": board.crawl_checkpoint,
                }
                for board in boards
            ],
        }

    async def request_reindex(
        self,
        session: AsyncSession,
        *,
        actor_id: int,
        source_key: str,
        confirmation: str,
    ) -> int:
        if confirmation != source_key:
            raise ValueError("reindex confirmation must exactly match the source key")
        job_id = await enqueue_job(
            session,
            job_type="reindex_source",
            dedupe_key=source_key,
            payload={"source_key": source_key},
            priority=150,
        )
        await self.audit(
            session,
            actor_id=actor_id,
            action="reindex.request",
            target=source_key,
            result="queued",
            metadata={"job_id": job_id},
        )
        return job_id

    async def deny_article(
        self,
        session: AsyncSession,
        *,
        actor_id: int,
        board_id: int,
        remote_article_id: int,
        reason: str,
        confirmation: str,
    ) -> None:
        source_key = article_source_key(board_id, remote_article_id)
        if confirmation != source_key:
            raise ValueError("denylist confirmation must exactly match the source key")
        actor_hash = self.require_owner(actor_id)
        article = await session.scalar(
            sa.select(Article).where(
                Article.board_id == board_id,
                Article.remote_article_id == remote_article_id,
            )
        )
        if article is None:
            await session.execute(
                insert(DenylistEntry)
                .values(
                    normalized_url=None,
                    source_key=source_key,
                    reason=reason,
                    actor_hash=actor_hash,
                    active=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[DenylistEntry.source_key],
                    index_where=sa.text("active"),
                )
            )
        else:
            await exclude_article(
                session,
                article=article,
                reason=reason,
                observed_at=utc_now(),
                actor_hash=actor_hash,
            )
        await self.audit(
            session,
            actor_id=actor_id,
            action="denylist.add",
            target=source_key,
            result="succeeded",
        )

    async def configure_channels(
        self,
        session: AsyncSession,
        *,
        actor_id: int,
        guild_id: int,
        channel_ids: tuple[int, ...],
        role_ids: tuple[int, ...] = (),
    ) -> None:
        self.require_owner(actor_id)
        if not channel_ids:
            raise ValueError("at least one allowed channel is required")
        await session.execute(
            insert(GuildSettings)
            .values(
                guild_id=guild_id,
                allowed_channel_ids=list(channel_ids),
                owner_id=self.owner_id,
                allowed_role_ids=list(role_ids),
            )
            .on_conflict_do_update(
                index_elements=[GuildSettings.guild_id],
                set_={
                    "allowed_channel_ids": list(channel_ids),
                    "owner_id": self.owner_id,
                    "allowed_role_ids": list(role_ids),
                    "updated_at": sa.func.now(),
                },
            )
        )
        await self.audit(
            session,
            actor_id=actor_id,
            action="channels.configure",
            target=str(guild_id),
            result="succeeded",
            metadata={"channel_count": len(channel_ids), "role_count": len(role_ids)},
        )
