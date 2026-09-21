from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.crawler.nexon_updates import (
    PATCH_NOTE_BOARD_ID,
    OfficialNexonClient,
    sync_recent_patch_notes,
)
from maple_chat.db.models import Article, Board, Chunk, OutboxEvent, SourceStatus
from maple_chat.indexing.embedding import EmbeddingSpec
from maple_chat.indexing.service import index_sanitized_sources

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


class FixedEmbedding:
    def __init__(self) -> None:
        self.spec = EmbeddingSpec("patch-note-test", "BAAI/bge-m3", "patch-note-test-commit")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _ in texts]


def listing_html(version: int) -> str:
    return f"""
    <div class="update_board"><ul>
      <li><a href="/News/Update/811?page=1">
        <em class="modify_common" data-modifytime="2026-08-20 11:2{version}">
          수정{version}
        </em> 클라이언트 1.2.418 업데이트 안내
      </a><dd>2026.08.20</dd></li>
      <li><a href="/News/Update/700?page=1">오래된 업데이트</a>
        <dd>2025.01.01</dd></li>
    </ul></div>
    """


@pytest.mark.asyncio
async def test_patch_note_sync_is_incremental_indexed_as_official_and_expires_old_window(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    state = {"version": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.casefold() == "/news/update":
            body = listing_html(state["version"])
        else:
            body = (
                '<div class="qs_text"><div class="new_board_con">'
                f"<h2>벨로나 변경 {state['version']}</h2><p>공식 변경 내용</p>"
                "</div></div>"
            )
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    observed_at = datetime(2026, 8, 25, tzinfo=UTC)
    async with OfficialNexonClient(
        "MapleChat integration collector",
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        async with factory() as session, session.begin():
            first = await sync_recent_patch_notes(
                session,
                client=client,
                observed_at=observed_at,
            )
        assert first.discovered == 1
        assert first.inserted == 1
        assert first.details_fetched == 1

        indexed = await index_sanitized_sources(
            factory,
            provider=FixedEmbedding(),
            activated_at=observed_at,
        )
        assert indexed.articles == 1
        async with factory() as session:
            chunk = await session.scalar(sa.select(Chunk).where(Chunk.active))
            assert chunk is not None
            assert chunk.metadata_json["source_authority"] == "official"
            assert chunk.metadata_json["source_kind"] == "official_patch_note"

        state["version"] = 2
        async with factory() as session, session.begin():
            changed = await sync_recent_patch_notes(
                session,
                client=client,
                observed_at=observed_at,
            )
        assert changed.updated == 1
        assert changed.details_fetched == 1

        async with factory() as session, session.begin():
            unchanged = await sync_recent_patch_notes(
                session,
                client=client,
                observed_at=observed_at,
            )
        assert unchanged.unchanged == 1
        assert unchanged.details_fetched == 0

        expired_at = datetime(2027, 8, 25, tzinfo=UTC)
        async with factory() as session, session.begin():
            expired = await sync_recent_patch_notes(
                session,
                client=client,
                observed_at=expired_at,
            )
        assert expired.discovered == 0
        assert expired.expired == 1

    async with factory() as session:
        article = await session.scalar(
            sa.select(Article).where(Article.board_id == PATCH_NOTE_BOARD_ID)
        )
        board = await session.get(Board, PATCH_NOTE_BOARD_ID)
        assert article is not None
        assert article.status == SourceStatus.DELETED
        assert board is not None
        assert board.crawl_checkpoint["since_days"] == 365
        assert await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 2
        assert (
            await session.scalar(sa.select(sa.func.count()).select_from(Chunk).where(Chunk.active))
            == 0
        )
