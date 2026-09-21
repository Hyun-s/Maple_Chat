from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.config import ProcessRole, Settings
from maple_chat.crawler.http import RespectfulInvenClient
from maple_chat.crawler.live import ingest_live_board_page
from maple_chat.crawler.policy import CrawlScope
from maple_chat.db.models import Article, Board, Chunk, Comment, GuildSettings, SourceStatus
from maple_chat.indexing.embedding import EmbeddingSpec
from maple_chat.indexing.service import index_sanitized_sources
from maple_chat.runtime import configure_or_load_channels
from maple_chat.scheduler.planner import seed_boards

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


class FixedEmbedding:
    def __init__(self, revision: str = "bge-live-test") -> None:
        self.spec = EmbeddingSpec(revision, "BAAI/bge-m3", f"{revision}-commit")
        self.batch_sizes: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [[1.0] + [0.0] * 1023 for _ in texts]


@pytest.mark.asyncio
async def test_reindex_preserves_replaced_chunk_as_inactive(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime(2026, 8, 5, tzinfo=UTC)
    async with factory() as session, session.begin():
        session.add(Board(board_id=2304, name="tips"))
        await session.flush()
        session.add(
            Article(
                board_id=2304,
                remote_article_id=99,
                url="https://www.inven.co.kr/board/maple/2304/99",
                title="before",
                sanitized_body="first sanitized body",
                first_observed_at=observed_at,
                last_observed_at=observed_at,
                status=SourceStatus.SANITIZED,
                content_hash="a" * 64,
            )
        )

    first = await index_sanitized_sources(
        factory,
        provider=FixedEmbedding(),
        activated_at=observed_at,
    )
    assert first.vectors_written == 1

    async with factory() as session, session.begin():
        article = await session.scalar(sa.select(Article))
        assert article is not None
        article.title = "after"
        article.sanitized_body = "second sanitized body"
        article.content_hash = "b" * 64
        article.status = SourceStatus.SANITIZED

    second = await index_sanitized_sources(
        factory,
        provider=FixedEmbedding(),
        activated_at=observed_at,
    )
    assert second.vectors_written == 1

    async with factory() as session:
        chunks = list(
            await session.scalars(
                sa.select(Chunk)
                .where(Chunk.source_key == "article:2304:99")
                .order_by(Chunk.created_at)
            )
        )
        assert len(chunks) == 2
        assert [chunk.active for chunk in chunks] == [False, True]
        assert chunks[0].chunk_id != chunks[1].chunk_id


def live_settings(tmp_path: Path) -> Settings:
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "status": "approved",
                "evidence_type": "legal_review",
                "approved_at": "2026-08-05T00:00:00Z",
                "approved_by": "fixture-reviewer",
                "scopes": ["approved_sample"],
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        role=ProcessRole.CRAWLER_WORKER,
        database_url=os.environ["G002_DATABASE_URL"],
        crawler_user_agent="MapleChat/0.1 fixture",
        crawler_contact="operator@example.invalid",
        pii_hash_salt="x" * 32,
        live_crawl_enabled=True,
        live_crawl_approval_file=approval,
    )


@pytest.mark.asyncio
async def test_rights_gated_live_http_to_index_pipeline_and_channel_bootstrap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    detail_requests = 0
    listing = Path("tests/fixtures/inven/listings/2304.html").read_bytes()
    article = Path("tests/fixtures/inven/articles/2304-2304001.html").read_text(encoding="utf-8")
    comment_payload = {
        "cmtcount": 2,
        "commentlist": [
            {
                "list": [
                    {
                        "__attr__": {"cmtidx": "10"},
                        "o_name": "answerer",
                        "o_comment": "안전한 답변",
                        "o_datetime": "2026-08-05 12:00:00",
                    },
                    {
                        "__attr__": {"cmtidx": "11"},
                        "o_name": "replier",
                        "o_comment": "보충 답글",
                        "o_datetime": "2026-08-05 12:01:00",
                    },
                ]
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_requests
        if request.method == "POST":
            assert b"act=list" in request.content
            return httpx.Response(200, json=comment_payload)
        if request.url.path.endswith("/2304001"):
            detail_requests += 1
            return httpx.Response(
                200,
                text=article,
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(
            200,
            content=listing,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    settings = live_settings(tmp_path)
    async with factory() as session, session.begin():
        await seed_boards(session)
        board = await session.get(Board, 2304)
        assert board is not None
        board.crawl_checkpoint = {
            "last_successful_page": 41,
            "next_backfill_page": 42,
            "backfill_complete": False,
        }
    async with (
        RespectfulInvenClient(
            settings,
            minimum_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as client,
        factory() as session,
        session.begin(),
    ):
        result = await ingest_live_board_page(
            session,
            client=client,
            board_id=2304,
            page=1,
            observed_at=datetime(2026, 8, 5, tzinfo=UTC),
            nickname_salt="x" * 32,
            scope=CrawlScope.APPROVED_SAMPLE,
            max_articles=1,
            include_comments=True,
            include_ocr=False,
            preserve_backfill_checkpoint=True,
        )
    assert result.ingested_articles == 1
    assert result.comments == 2
    assert result.skipped_existing == 0
    assert result.unseen_articles == 1

    async with (
        RespectfulInvenClient(
            settings,
            minimum_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as client,
        factory() as session,
        session.begin(),
    ):
        repeated = await ingest_live_board_page(
            session,
            client=client,
            board_id=2304,
            page=1,
            observed_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
            nickname_salt="x" * 32,
            scope=CrawlScope.APPROVED_SAMPLE,
            max_articles=1,
            include_comments=True,
            include_ocr=False,
            preserve_backfill_checkpoint=True,
            skip_existing=True,
        )
    assert repeated.ingested_articles == 0
    assert repeated.comments == 0
    assert repeated.skipped_existing == 1
    assert repeated.unseen_articles == 0
    assert detail_requests == 1

    async with (
        RespectfulInvenClient(
            settings,
            minimum_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as client,
        factory() as session,
        session.begin(),
    ):
        known_backfill_page = await ingest_live_board_page(
            session,
            client=client,
            board_id=2304,
            page=1,
            observed_at=datetime(2026, 8, 5, 2, tzinfo=UTC),
            nickname_salt="x" * 32,
            scope=CrawlScope.APPROVED_SAMPLE,
            max_articles=1,
            include_comments=True,
            include_ocr=False,
            skip_existing=True,
        )
    assert known_backfill_page.unseen_articles == 0
    async with factory() as session:
        board = await session.get(Board, 2304)
        assert board is not None
        assert board.crawl_checkpoint["next_backfill_page"] == 2
        assert board.crawl_checkpoint["backfill_complete"] is False

    provider = FixedEmbedding()
    indexed = await index_sanitized_sources(
        factory,
        provider=provider,
        activated_at=datetime(2026, 8, 5, tzinfo=UTC),
        batch_size=2,
    )
    assert indexed.articles == 1
    assert indexed.comments == 2
    assert indexed.vectors_written == 3
    assert provider.batch_sizes == [1, 2]

    second = await index_sanitized_sources(
        factory,
        provider=FixedEmbedding(),
        activated_at=datetime(2026, 8, 5, tzinfo=UTC),
        batch_size=2,
    )
    assert second.vectors_written == 0

    replacement = await index_sanitized_sources(
        factory,
        provider=FixedEmbedding("bge-live-replacement"),
        activated_at=datetime(2026, 8, 5, tzinfo=UTC),
        batch_size=2,
    )
    assert replacement.vectors_written == 3

    channels = await configure_or_load_channels(
        factory,
        guild_id=123,
        owner_id=456,
        configured_channel_ids=(10, 20),
    )
    assert channels == {10, 20}
    assert await configure_or_load_channels(
        factory,
        guild_id=123,
        owner_id=456,
        configured_channel_ids=(),
    ) == {10, 20}

    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Chunk)) == 3
        assert await session.scalar(sa.select(sa.func.count()).select_from(Comment)) == 2
        article_row = await session.scalar(sa.select(Article))
        assert article_row is not None
        assert article_row.status == SourceStatus.INDEXED
        assert article_row.comment_count == 2
        guild = await session.get(GuildSettings, 123)
        assert guild is not None
        assert guild.allowed_channel_ids == [10, 20]
        board = await session.get(Board, 2304)
        assert board is not None
        assert board.crawl_checkpoint["last_successful_page"] == 1
        assert board.crawl_checkpoint["next_backfill_page"] == 2
        assert board.crawl_checkpoint["last_refresh_at"] == "2026-08-05T01:00:00+00:00"
