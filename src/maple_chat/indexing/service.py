"""Operational indexing service that turns sanitized database rows into active vectors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.crawler.nexon_updates import PATCH_NOTE_BOARD_ID
from maple_chat.db.models import (
    Article,
    Board,
    Category,
    Chunk,
    ChunkEmbedding,
    Comment,
    EmbeddingRevision,
    MediaAsset,
    SourceStatus,
    SourceType,
)
from maple_chat.db.repositories import activate_embedding_revision, article_source_key
from maple_chat.indexing.chunking import (
    ChunkDraft,
    SourceDocument,
    chunk_document,
    comment_branch_document,
)
from maple_chat.indexing.embedding import EmbeddingProvider, EmbeddingSpec, index_chunk_batch


@dataclass(frozen=True, slots=True)
class IndexResult:
    articles: int
    comments: int
    ocr_assets: int
    vectors_written: int
    revision: str


async def _ensure_revision(
    session: AsyncSession,
    spec: EmbeddingSpec,
) -> tuple[EmbeddingRevision, bool]:
    revision = await session.scalar(
        sa.select(EmbeddingRevision).where(EmbeddingRevision.name == spec.name)
    )
    if revision is None:
        revision = EmbeddingRevision(
            name=spec.name,
            model=spec.model,
            model_commit=spec.model_commit,
            dimension=spec.dimension,
            normalized=spec.normalized,
            distance=spec.distance,
        )
        session.add(revision)
        await session.flush()
    if (
        revision.model != spec.model
        or revision.model_commit != spec.model_commit
        or revision.dimension != spec.dimension
        or revision.normalized != spec.normalized
        or revision.distance != spec.distance
    ):
        raise RuntimeError("stored embedding revision is incompatible with configured model")
    return revision, not revision.active


async def _replace_source_chunks(
    session: AsyncSession,
    *,
    source_key: str,
    active_chunk_ids: tuple[str, ...],
) -> None:
    stale_ids = sa.select(Chunk.chunk_id).where(
        Chunk.source_key == source_key,
        Chunk.chunk_id.not_in(active_chunk_ids),
    )
    await session.execute(
        sa.update(Chunk).where(Chunk.chunk_id.in_(stale_ids)).values(active=False)
    )
    await session.execute(
        sa.update(ChunkEmbedding).where(ChunkEmbedding.chunk_id.in_(stale_ids)).values(active=False)
    )


async def index_sanitized_sources(
    factory: async_sessionmaker[AsyncSession],
    *,
    provider: EmbeddingProvider,
    activated_at: datetime,
    batch_size: int = 32,
) -> IndexResult:
    if batch_size < 1 or batch_size > 256:
        raise ValueError("index batch size must be between 1 and 256")
    spec = provider.spec
    spec.validate()
    article_count = 0
    comment_count = 0
    ocr_count = 0
    vectors_written = 0

    async with factory() as session, session.begin():
        revision, rebuild_revision = await _ensure_revision(session, spec)
        revision_id = revision.id
    article_statuses = (
        [SourceStatus.SANITIZED, SourceStatus.INDEXED]
        if rebuild_revision
        else [SourceStatus.SANITIZED]
    )
    comment_statuses = (
        [SourceStatus.SANITIZED, SourceStatus.INDEXED]
        if rebuild_revision
        else [SourceStatus.SANITIZED]
    )
    media_statuses = ["ocr_ready", "indexed"] if rebuild_revision else ["ocr_ready"]

    article_cursor = 0
    while True:
        async with factory() as session, session.begin():
            loaded_revision = await session.get(EmbeddingRevision, revision_id)
            if loaded_revision is None:
                raise RuntimeError("embedding revision disappeared")
            article_rows = (
                await session.execute(
                    sa.select(Article, Board.name, Category.remote_name)
                    .join(Board, Board.board_id == Article.board_id)
                    .outerjoin(Category, Category.id == Article.category_id)
                    .where(
                        Article.status.in_(article_statuses),
                        Article.id > article_cursor if rebuild_revision else sa.true(),
                    )
                    .order_by(Article.id)
                    .limit(batch_size)
                    .with_for_update(of=Article, skip_locked=True)
                )
            ).all()
            if not article_rows:
                break
            batch_drafts: list[ChunkDraft] = []
            for article, board_name, category_name in article_rows:
                article_cursor = max(article_cursor, article.id)
                source_key = article_source_key(article.board_id, article.remote_article_id)
                source_metadata = {
                    "url": article.url,
                    "view_count": article.view_count,
                    "recommendation_count": article.recommendation_count,
                    "comment_count": article.comment_count,
                }
                if article.board_id == PATCH_NOTE_BOARD_ID:
                    source_metadata.update(
                        {
                            "source_kind": "official_patch_note",
                            "source_authority": "official",
                            "publisher": "NEXON Korea",
                        }
                    )
                drafts = chunk_document(
                    SourceDocument(
                        source_type=SourceType.ARTICLE,
                        source_key=source_key,
                        title=article.title,
                        board=board_name,
                        category=category_name,
                        published_at=article.published_at,
                        text=article.sanitized_body,
                        metadata=source_metadata,
                    )
                )
                await _replace_source_chunks(
                    session,
                    source_key=source_key,
                    active_chunk_ids=tuple(draft.chunk_id for draft in drafts),
                )
                batch_drafts.extend(drafts)
                article.status = SourceStatus.INDEXED
                article_count += 1
            if batch_drafts:
                vectors_written += await index_chunk_batch(
                    session,
                    drafts=tuple(batch_drafts),
                    revision=loaded_revision,
                    provider=provider,
                )

    comment_cursor = 0
    while True:
        async with factory() as session, session.begin():
            loaded_revision = await session.get(EmbeddingRevision, revision_id)
            if loaded_revision is None:
                raise RuntimeError("embedding revision disappeared")
            comment_rows = (
                await session.execute(
                    sa.select(Comment, Article, Board.name, Category.remote_name)
                    .join(Article, Article.id == Comment.article_id)
                    .join(Board, Board.board_id == Article.board_id)
                    .outerjoin(Category, Category.id == Article.category_id)
                    .where(
                        Comment.status.in_(comment_statuses),
                        Article.status.in_([SourceStatus.SANITIZED, SourceStatus.INDEXED]),
                        Comment.id > comment_cursor if rebuild_revision else sa.true(),
                    )
                    .order_by(Comment.id)
                    .limit(batch_size)
                    .with_for_update(of=Comment, skip_locked=True)
                )
            ).all()
            if not comment_rows:
                break
            batch_drafts = []
            for comment, article, board_name, category_name in comment_rows:
                comment_cursor = max(comment_cursor, comment.id)
                article_key = article_source_key(article.board_id, article.remote_article_id)
                source_key = f"comment:{article_key}:{comment.remote_comment_id}"
                document = comment_branch_document(
                    source_key=source_key,
                    article_title=article.title,
                    article_context=article.sanitized_body[:800],
                    board=board_name,
                    category=category_name,
                    published_at=comment.published_at or article.published_at,
                    messages=(comment.sanitized_body,),
                    metadata={
                        "url": article.url,
                        "article_source_key": article_key,
                        "parent_comment_id": comment.parent_remote_comment_id,
                        "score": comment.score,
                    },
                )
                drafts = chunk_document(document)
                await _replace_source_chunks(
                    session,
                    source_key=source_key,
                    active_chunk_ids=tuple(draft.chunk_id for draft in drafts),
                )
                batch_drafts.extend(drafts)
                comment.status = SourceStatus.INDEXED
                comment_count += 1
            if batch_drafts:
                vectors_written += await index_chunk_batch(
                    session,
                    drafts=tuple(batch_drafts),
                    revision=loaded_revision,
                    provider=provider,
                )

    media_cursor = 0
    while True:
        async with factory() as session, session.begin():
            loaded_revision = await session.get(EmbeddingRevision, revision_id)
            if loaded_revision is None:
                raise RuntimeError("embedding revision disappeared")
            media_rows = (
                await session.execute(
                    sa.select(MediaAsset, Article, Board.name, Category.remote_name)
                    .join(Article, Article.id == MediaAsset.article_id)
                    .join(Board, Board.board_id == Article.board_id)
                    .outerjoin(Category, Category.id == Article.category_id)
                    .where(
                        MediaAsset.fetch_status.in_(media_statuses),
                        MediaAsset.ocr_text.is_not(None),
                        MediaAsset.id > media_cursor if rebuild_revision else sa.true(),
                    )
                    .order_by(MediaAsset.id)
                    .limit(batch_size)
                    .with_for_update(of=MediaAsset, skip_locked=True)
                )
            ).all()
            if not media_rows:
                break
            batch_drafts = []
            for asset, article, board_name, category_name in media_rows:
                media_cursor = max(media_cursor, asset.id)
                source_key = (
                    f"ocr:{article_source_key(article.board_id, article.remote_article_id)}:"
                    f"{asset.source_url_hash[:16]}"
                )
                drafts = chunk_document(
                    SourceDocument(
                        source_type=SourceType.OCR,
                        source_key=source_key,
                        title=article.title,
                        board=board_name,
                        category=category_name,
                        published_at=article.published_at,
                        text=asset.ocr_text or "",
                        metadata={
                            "url": article.url,
                            "image_url": asset.source_url,
                            "ocr_confidence": asset.ocr_confidence,
                        },
                    )
                )
                await _replace_source_chunks(
                    session,
                    source_key=source_key,
                    active_chunk_ids=tuple(draft.chunk_id for draft in drafts),
                )
                batch_drafts.extend(drafts)
                asset.fetch_status = "indexed"
                ocr_count += 1
            if batch_drafts:
                vectors_written += await index_chunk_batch(
                    session,
                    drafts=tuple(batch_drafts),
                    revision=loaded_revision,
                    provider=provider,
                )

    async with factory() as session, session.begin():
        loaded_revision = await session.get(EmbeddingRevision, revision_id)
        if loaded_revision is None:
            raise RuntimeError("embedding revision disappeared")
        if not loaded_revision.active:
            await activate_embedding_revision(
                session, loaded_revision.id, activated_at=activated_at
            )

    return IndexResult(
        articles=article_count,
        comments=comment_count,
        ocr_assets=ocr_count,
        vectors_written=vectors_written,
        revision=spec.name,
    )
