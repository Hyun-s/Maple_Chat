from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import (
    Article,
    Board,
    Chunk,
    ChunkEmbedding,
    Comment,
    CrawlJob,
    DenylistEntry,
    EmbeddingRevision,
    JobState,
    MediaAsset,
    OutboxEvent,
    SourceStatus,
    SourceType,
    Tombstone,
)
from maple_chat.db.repositories import (
    ArticleRecord,
    SourceDenied,
    activate_embedding_revision,
    exclude_article,
    upsert_article_with_outbox,
)
from maple_chat.jobs.queue import (
    JobLease,
    enqueue_job,
    fail_job,
    lease_jobs,
    reclaim_expired_leases,
)
from maple_chat.jobs.worker import LeaseWorker
from maple_chat.ops.reconcile import reconcile_startup
from maple_chat.time import utc_now

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


async def seed_article(factory: async_sessionmaker[AsyncSession]) -> Article:
    now = utc_now()
    async with factory() as session, session.begin():
        session.add(Board(board_id=2304, name="tips"))
        article, _ = await upsert_article_with_outbox(
            session,
            ArticleRecord(
                board_id=2304,
                remote_article_id=1,
                url="https://www.inven.co.kr/board/maple/2304/1",
                title="safe title",
                sanitized_body="safe body",
                content_hash="a" * 64,
                observed_at=now,
            ),
        )
    return article


@pytest.mark.asyncio
async def test_article_and_job_upserts_are_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = utc_now()
    async with factory() as session, session.begin():
        session.add(Board(board_id=2304, name="tips"))
    ids: list[int] = []
    for _ in range(10):
        async with factory() as session, session.begin():
            article, _ = await upsert_article_with_outbox(
                session,
                ArticleRecord(
                    board_id=2304,
                    remote_article_id=99,
                    url="https://www.inven.co.kr/board/maple/2304/99",
                    title="safe title",
                    sanitized_body="safe body",
                    content_hash="b" * 64,
                    observed_at=now,
                ),
            )
            ids.append(
                await enqueue_job(
                    session,
                    job_type="chunk",
                    dedupe_key="article:2304:99",
                    payload={"article_id": article.id},
                )
            )
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Article)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(CrawlJob)) == 1
    assert len(set(ids)) == 1


@pytest.mark.asyncio
async def test_workers_compete_for_one_lease_and_expiry_is_reclaimed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        job_id = await enqueue_job(
            session,
            job_type="crawl",
            dedupe_key="board:2304:page:1",
            payload={"board_id": 2304, "page": 1},
        )
    now = utc_now()
    first, second = await asyncio.gather(
        lease_jobs(factory, worker_id="worker-a", now=now, lease_seconds=1),
        lease_jobs(factory, worker_id="worker-b", now=now, lease_seconds=1),
    )
    assert sorted((len(first), len(second))) == [0, 1]

    async with factory() as session, session.begin():
        assert await reclaim_expired_leases(session, now=now + timedelta(seconds=2)) == (1, 0)
    async with factory() as session:
        job = await session.get(CrawlJob, job_id)
        assert job is not None
        assert job.state == JobState.QUEUED
        assert job.lease_owner is None


@pytest.mark.asyncio
async def test_failure_dead_letters_at_bound_and_revision_cutover_is_atomic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = utc_now()
    async with factory() as session, session.begin():
        job_id = await enqueue_job(
            session,
            job_type="crawl",
            dedupe_key="terminal",
            payload={},
            max_attempts=1,
        )
        first = EmbeddingRevision(
            name="first",
            model="BAAI/bge-m3",
            model_commit="first",
            dimension=1024,
            normalized=True,
            distance="cosine",
        )
        second = EmbeddingRevision(
            name="second",
            model="BAAI/bge-m3",
            model_commit="second",
            dimension=1024,
            normalized=True,
            distance="cosine",
        )
        session.add_all([first, second])
        await session.flush()
        first_id, second_id = first.id, second.id
        await activate_embedding_revision(session, first_id, activated_at=now)

    lease_at = utc_now() + timedelta(seconds=1)
    lease = await lease_jobs(factory, worker_id="worker", now=lease_at)
    assert [item.id for item in lease] == [job_id]
    async with factory() as session, session.begin():
        assert (
            await fail_job(
                session,
                job_id=job_id,
                worker_id="worker",
                error_code="schema_drift",
                now=lease_at,
            )
            == JobState.DEAD_LETTER
        )
        await activate_embedding_revision(session, second_id, activated_at=now)

    async with factory() as session:
        active = list(
            await session.scalars(
                sa.select(EmbeddingRevision).where(EmbeddingRevision.active.is_(True))
            )
        )
        assert [revision.id for revision in active] == [second_id]
        job = await session.get(CrawlJob, job_id)
        assert job is not None
        assert job.state == JobState.DEAD_LETTER


@pytest.mark.asyncio
async def test_tombstone_and_denylist_exclusion_is_transactional(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    article = await seed_article(factory)
    now = utc_now()
    async with factory() as session, session.begin():
        revision = EmbeddingRevision(
            name="bge-m3-test",
            model="BAAI/bge-m3",
            model_commit="fixture",
            dimension=1024,
            normalized=True,
            distance="cosine",
            active=True,
            activated_at=now,
        )
        chunk = Chunk(
            chunk_id="c" * 64,
            source_type=SourceType.ARTICLE,
            source_key="article:2304:1",
            ordinal=0,
            text="safe chunk",
            token_count=2,
            metadata_json={},
            chunking_revision="v1",
            active=True,
        )
        comment_chunk = Chunk(
            chunk_id="d" * 64,
            source_type=SourceType.COMMENT,
            source_key="comment:article:2304:1:1",
            ordinal=0,
            text="comment chunk",
            token_count=2,
            metadata_json={},
            chunking_revision="v1",
            active=True,
        )
        ocr_chunk = Chunk(
            chunk_id="e" * 64,
            source_type=SourceType.OCR,
            source_key="ocr:article:2304:1:asset:1",
            ordinal=0,
            text="ocr chunk",
            token_count=2,
            metadata_json={},
            chunking_revision="v1",
            active=True,
        )
        comment = Comment(
            article_id=article.id,
            remote_comment_id=1,
            sanitized_body="comment body",
            first_observed_at=now,
            last_observed_at=now,
            status=SourceStatus.SANITIZED,
            content_hash="f" * 64,
        )
        media = MediaAsset(
            article_id=article.id,
            source_url="https://www.inven.co.kr/upload/fixture.png",
            source_url_hash="1" * 64,
            fetch_status="ocr_succeeded",
            ocr_text="ocr body",
        )
        session.add_all([revision, chunk, comment_chunk, ocr_chunk, comment, media])
        await session.flush()
        session.add_all(
            [
                ChunkEmbedding(
                    chunk_id=item.chunk_id,
                    revision_id=revision.id,
                    embedding=[0.0] * 1024,
                    active=True,
                )
                for item in (chunk, comment_chunk, ocr_chunk)
            ]
        )

    async with factory() as session, session.begin():
        persisted = await session.get(Article, article.id)
        assert persisted is not None
        await exclude_article(
            session,
            article=persisted,
            reason="rights_request",
            observed_at=now,
            actor_hash="owner-hash",
        )
        await session.flush()
        assert persisted.status == SourceStatus.DENIED
        await session.rollback()

    async with factory() as session:
        persisted = await session.get(Article, article.id)
        chunk = await session.get(Chunk, "c" * 64)
        assert persisted is not None
        assert persisted.status == SourceStatus.SANITIZED
        assert chunk is not None
        assert chunk.active is True
        assert await session.scalar(sa.select(sa.func.count()).select_from(Tombstone)) == 0

    async with factory() as session, session.begin():
        persisted = await session.get(Article, article.id)
        assert persisted is not None
        await exclude_article(
            session,
            article=persisted,
            reason="rights_request",
            observed_at=now,
            actor_hash="owner-hash",
        )

    async with factory() as session:
        persisted = await session.get(Article, article.id)
        chunks = list(await session.scalars(sa.select(Chunk).order_by(Chunk.chunk_id)))
        assert persisted is not None
        assert persisted.status == SourceStatus.DENIED
        assert persisted.sanitized_body == ""
        assert all(chunk.active is False for chunk in chunks)
        embeddings = list(await session.scalars(sa.select(ChunkEmbedding)))
        assert all(item.active is False for item in embeddings)
        comment = await session.scalar(sa.select(Comment))
        media = await session.scalar(sa.select(MediaAsset))
        assert comment is not None
        assert comment.sanitized_body == ""
        assert media is not None
        assert media.ocr_text is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(Tombstone)) == 1
        assert await session.scalar(sa.select(sa.func.count()).select_from(DenylistEntry)) == 1


@pytest.mark.asyncio
async def test_startup_reconciliation_reapplies_denylist_and_expired_leases(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    article = await seed_article(factory)
    async with factory() as session, session.begin():
        session.add(
            DenylistEntry(
                normalized_url=article.url,
                source_key="article:2304:1",
                reason="rights_request",
                actor_hash="owner-hash",
                active=True,
            )
        )
        await enqueue_job(
            session,
            job_type="reconcile",
            dedupe_key="expired",
            payload={},
        )
    lease_at = utc_now() + timedelta(seconds=1)
    leases = await lease_jobs(factory, worker_id="gone", now=lease_at, lease_seconds=1)
    assert len(leases) == 1

    async with factory() as session, session.begin():
        result = await reconcile_startup(session, now=lease_at + timedelta(seconds=2))
    assert result.exclusions == 1
    assert result.reclaimed_jobs == 1
    async with factory() as session:
        persisted = await session.get(Article, article.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.DENIED


@pytest.mark.asyncio
async def test_active_denylist_prevents_source_reinsertion(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    article = await seed_article(factory)
    now = utc_now()
    async with factory() as session, session.begin():
        persisted = await session.get(Article, article.id)
        assert persisted is not None
        await exclude_article(
            session,
            article=persisted,
            reason="rights_request",
            observed_at=now,
            actor_hash="owner-hash",
        )
    async with factory() as session, session.begin():
        with pytest.raises(SourceDenied, match="denylist"):
            await upsert_article_with_outbox(
                session,
                ArticleRecord(
                    board_id=2304,
                    remote_article_id=1,
                    url=article.url,
                    title="attempted revival",
                    sanitized_body="must not persist",
                    content_hash="9" * 64,
                    observed_at=now,
                ),
            )
    async with factory() as session:
        persisted = await session.get(Article, article.id)
        assert persisted is not None
        assert persisted.status == SourceStatus.DENIED
        assert persisted.sanitized_body == ""


@pytest.mark.asyncio
async def test_lease_worker_acknowledges_success_and_retries_handler_failures(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    handled: list[int] = []

    async def success(lease: JobLease) -> None:
        handled.append(lease.id)

    async def failure(lease: JobLease) -> None:
        raise RuntimeError("fixture failure")

    async with factory() as session, session.begin():
        success_id = await enqueue_job(
            session,
            job_type="success",
            dedupe_key="worker-success",
            payload={},
        )
        failure_id = await enqueue_job(
            session,
            job_type="failure",
            dedupe_key="worker-failure",
            payload={},
        )
    worker = LeaseWorker(
        factory,
        worker_id="test-worker",
        handlers={"success": success, "failure": failure},
    )
    tick = await worker.tick(limit=2)
    assert tick.leased == 2
    assert tick.succeeded == 1
    assert tick.failed == 1
    assert handled == [success_id]
    async with factory() as session:
        success_job = await session.get(CrawlJob, success_id)
        failed_job = await session.get(CrawlJob, failure_id)
        assert success_job is not None
        assert success_job.state == JobState.SUCCEEDED
        assert failed_job is not None
        assert failed_job.state == JobState.QUEUED
        assert failed_job.last_error_code == "handler_failed:RuntimeError"
