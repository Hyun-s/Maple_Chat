"""Transactional idempotent source and exclusion operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import (
    Article,
    Category,
    Chunk,
    ChunkEmbedding,
    Comment,
    DenylistEntry,
    EmbeddingRevision,
    MediaAsset,
    OutboxEvent,
    SourceStatus,
    SourceType,
    Tombstone,
)


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    board_id: int
    remote_article_id: int
    url: str
    title: str
    sanitized_body: str
    content_hash: str
    observed_at: datetime
    category_id: int | None = None
    author_hash: str | None = None
    published_at: datetime | None = None
    source_modified_at: datetime | None = None
    view_count: int = 0
    recommendation_count: int = 0
    comment_count: int = 0


def article_source_key(board_id: int, remote_article_id: int) -> str:
    return f"article:{board_id}:{remote_article_id}"


class SourceDenied(RuntimeError):
    """A durable denylist entry forbids source reinsertion."""


async def upsert_category(
    session: AsyncSession,
    *,
    board_id: int,
    remote_name: str,
    observed_at: datetime,
) -> int:
    category_id = await session.scalar(
        insert(Category)
        .values(
            board_id=board_id,
            remote_name=remote_name,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        .on_conflict_do_update(
            index_elements=[Category.board_id, Category.remote_name],
            set_={"last_seen_at": observed_at},
        )
        .returning(Category.id)
    )
    if category_id is None:
        raise RuntimeError("upserted category disappeared")
    return category_id


@dataclass(frozen=True, slots=True)
class CommentRecord:
    remote_comment_id: int
    parent_remote_comment_id: int | None
    sanitized_body: str
    content_hash: str
    observed_at: datetime
    author_hash: str | None = None
    published_at: datetime | None = None
    score: int = 0
    deleted: bool = False


async def upsert_comment_with_outbox(
    session: AsyncSession,
    *,
    article_id: int,
    article_source: str,
    record: CommentRecord,
) -> tuple[int, bool]:
    existing = await session.scalar(
        sa.select(Comment)
        .where(
            Comment.article_id == article_id,
            Comment.remote_comment_id == record.remote_comment_id,
        )
        .with_for_update()
    )
    changed = existing is None or existing.content_hash != record.content_hash
    status = SourceStatus.DELETED if record.deleted else SourceStatus.SANITIZED
    comment_id = await session.scalar(
        insert(Comment)
        .values(
            article_id=article_id,
            remote_comment_id=record.remote_comment_id,
            parent_remote_comment_id=record.parent_remote_comment_id,
            author_hash=record.author_hash,
            sanitized_body="" if record.deleted else record.sanitized_body,
            published_at=record.published_at,
            first_observed_at=record.observed_at,
            last_observed_at=record.observed_at,
            score=record.score,
            status=status,
            content_hash=record.content_hash,
        )
        .on_conflict_do_update(
            index_elements=[Comment.article_id, Comment.remote_comment_id],
            set_={
                "parent_remote_comment_id": record.parent_remote_comment_id,
                "author_hash": record.author_hash,
                "sanitized_body": "" if record.deleted else record.sanitized_body,
                "published_at": record.published_at,
                "last_observed_at": record.observed_at,
                "score": record.score,
                "status": status,
                "content_hash": record.content_hash,
            },
        )
        .returning(Comment.id)
    )
    if comment_id is None:
        raise RuntimeError("upserted comment disappeared")
    if changed:
        source_key = f"comment:{article_source}:{record.remote_comment_id}"
        await session.execute(
            insert(OutboxEvent)
            .values(
                topic="comment.sanitized" if not record.deleted else "comment.deleted",
                aggregate_key=source_key,
                dedupe_key=f"{source_key}:{record.content_hash}",
                payload={
                    "comment_id": comment_id,
                    "source_key": source_key,
                    "content_hash": record.content_hash,
                },
            )
            .on_conflict_do_nothing(index_elements=[OutboxEvent.dedupe_key])
        )
    return comment_id, changed


async def upsert_article_with_outbox(
    session: AsyncSession,
    record: ArticleRecord,
) -> tuple[Article, bool]:
    source_key = article_source_key(record.board_id, record.remote_article_id)
    denied = await session.scalar(
        sa.select(sa.literal(True)).where(
            sa.exists().where(
                DenylistEntry.source_key == source_key,
                DenylistEntry.active.is_(True),
            )
        )
    )
    if denied:
        raise SourceDenied("source is blocked by an active denylist entry")
    article = await session.scalar(
        sa.select(Article)
        .where(
            Article.board_id == record.board_id,
            Article.remote_article_id == record.remote_article_id,
        )
        .with_for_update()
    )
    if article is None:
        inserted_id = await session.scalar(
            insert(Article)
            .values(
                board_id=record.board_id,
                remote_article_id=record.remote_article_id,
                category_id=record.category_id,
                url=record.url,
                title=record.title,
                author_hash=record.author_hash,
                sanitized_body=record.sanitized_body,
                published_at=record.published_at,
                source_modified_at=record.source_modified_at,
                first_observed_at=record.observed_at,
                last_observed_at=record.observed_at,
                view_count=record.view_count,
                recommendation_count=record.recommendation_count,
                comment_count=record.comment_count,
                status=SourceStatus.SANITIZED,
                content_hash=record.content_hash,
            )
            .on_conflict_do_nothing(index_elements=[Article.board_id, Article.remote_article_id])
            .returning(Article.id)
        )
        lookup = (
            Article.id == inserted_id
            if inserted_id is not None
            else sa.and_(
                Article.board_id == record.board_id,
                Article.remote_article_id == record.remote_article_id,
            )
        )
        article = await session.scalar(sa.select(Article).where(lookup).with_for_update())
        if article is None:
            raise RuntimeError("upserted article disappeared")
        changed = inserted_id is not None
    else:
        changed = article.content_hash != record.content_hash
        article.category_id = record.category_id
        article.last_observed_at = record.observed_at
        article.view_count = record.view_count
        article.recommendation_count = record.recommendation_count
        article.comment_count = record.comment_count
        if changed:
            article.url = record.url
            article.title = record.title
            article.author_hash = record.author_hash
            article.sanitized_body = record.sanitized_body
            article.published_at = record.published_at
            article.source_modified_at = record.source_modified_at
            article.status = SourceStatus.SANITIZED
            article.content_hash = record.content_hash
            article.updated_at = record.observed_at

    if changed:
        session.add(
            OutboxEvent(
                topic="article.sanitized",
                aggregate_key=source_key,
                dedupe_key=f"{source_key}:{record.content_hash}",
                payload={
                    "article_id": article.id,
                    "source_key": source_key,
                    "content_hash": record.content_hash,
                },
            )
        )
    await session.flush()
    return article, changed


async def activate_embedding_revision(
    session: AsyncSession,
    revision_id: int,
    *,
    activated_at: datetime,
) -> None:
    """Atomically make one compatible embedding revision searchable."""
    await session.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtext("embedding-revision")))
    )
    revision = await session.get(EmbeddingRevision, revision_id, with_for_update=True)
    if revision is None:
        raise LookupError("embedding revision not found")
    if revision.dimension != 1024 or revision.distance != "cosine":
        raise ValueError("embedding revision is incompatible with the active vector index")
    await session.execute(
        sa.update(EmbeddingRevision)
        .where(EmbeddingRevision.active.is_(True), EmbeddingRevision.id != revision_id)
        .values(active=False)
    )
    revision.active = True
    revision.activated_at = activated_at
    await session.flush()


async def exclude_article(
    session: AsyncSession,
    *,
    article: Article,
    reason: str,
    observed_at: datetime,
    actor_hash: str | None = None,
) -> None:
    source_key = article_source_key(article.board_id, article.remote_article_id)
    article.status = SourceStatus.DENIED if actor_hash else SourceStatus.DELETED
    article.sanitized_body = ""
    article.author_hash = None
    article.content_hash = "0" * 64
    chunk_ids = sa.select(Chunk.chunk_id).where(
        sa.or_(
            sa.and_(
                Chunk.source_type == SourceType.ARTICLE,
                Chunk.source_key == source_key,
            ),
            Chunk.source_key.like(f"comment:{source_key}:%"),
            Chunk.source_key.like(f"ocr:{source_key}:%"),
        )
    )
    await session.execute(
        sa.update(Chunk).where(Chunk.chunk_id.in_(chunk_ids)).values(active=False)
    )
    await session.execute(
        sa.update(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(chunk_ids)).values(active=False)
    )
    await session.execute(
        sa.update(Comment)
        .where(Comment.article_id == article.id)
        .values(
            sanitized_body="",
            author_hash=None,
            status=SourceStatus.DENIED if actor_hash else SourceStatus.DELETED,
        )
    )
    await session.execute(
        sa.update(MediaAsset)
        .where(MediaAsset.article_id == article.id)
        .values(ocr_text=None, fetch_status="excluded")
    )
    await session.execute(
        insert(Tombstone)
        .values(
            source_type=SourceType.ARTICLE,
            source_key=source_key,
            deleted_observed_at=observed_at,
            reason=reason,
        )
        .on_conflict_do_update(
            index_elements=[Tombstone.source_type, Tombstone.source_key],
            set_={"deleted_observed_at": observed_at, "reason": reason},
        )
    )
    if actor_hash:
        await session.execute(
            insert(DenylistEntry)
            .values(
                normalized_url=article.url,
                source_key=source_key,
                reason=reason,
                actor_hash=actor_hash,
                active=True,
            )
            .on_conflict_do_update(
                index_elements=[DenylistEntry.source_key],
                index_where=sa.text("active"),
                set_={
                    "normalized_url": article.url,
                    "reason": reason,
                    "actor_hash": actor_hash,
                    "active": True,
                    "updated_at": sa.func.now(),
                },
            )
        )
    await session.flush()
