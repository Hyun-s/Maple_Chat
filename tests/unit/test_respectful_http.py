from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from maple_chat.config import ProcessRole, Settings
from maple_chat.crawler.circuit import CrawlCircuitOpen
from maple_chat.crawler.http import RespectfulInvenClient
from maple_chat.crawler.policy import CrawlScope, LiveCrawlDenied


def settings(tmp_path: Path, *, live: bool = True) -> Settings:
    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "status": "approved",
                "evidence_type": "legal_review",
                "approved_at": "2026-08-04T00:00:00Z",
                "approved_by": "fixture-reviewer",
                "scopes": ["approved_sample"],
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        role=ProcessRole.CRAWLER_WORKER,
        database_url="postgresql+asyncpg://fixture@localhost/db",
        crawler_user_agent="MapleChat/0.1 fixture",
        crawler_contact="operator@example.invalid",
        pii_hash_salt="x" * 32,
        live_crawl_enabled=live,
        live_crawl_approval_file=approval,
    )


@pytest.mark.asyncio
async def test_redirect_is_reauthorized_and_identity_headers_are_sent(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["user-agent"].startswith("MapleChat/")
        if request.url.path.endswith("/2304"):
            return httpx.Response(302, headers={"location": "/board/maple/2304/1"})
        return httpx.Response(200, content=b"safe fixture", headers={"content-type": "text/html"})

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.get(
            "https://www.inven.co.kr/board/maple/2304",
            scope=CrawlScope.APPROVED_SAMPLE,
        )
    assert result.content == b"safe fixture"
    assert calls == [
        "https://www.inven.co.kr/board/maple/2304",
        "https://www.inven.co.kr/board/maple/2304/1",
    ]


@pytest.mark.asyncio
async def test_redirect_to_unapproved_host_is_denied_before_second_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://evil.example.invalid/value"})

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(LiveCrawlDenied, match="allowlisted"):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )

    assert calls == 1


@pytest.mark.asyncio
async def test_repeated_403_and_captcha_open_the_circuit(tmp_path: Path) -> None:
    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(denied),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )
        with pytest.raises(CrawlCircuitOpen, match="repeated_403"):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )

    def captcha(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>reCAPTCHA</html>")

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(captcha),
    ) as client:
        with pytest.raises(CrawlCircuitOpen, match="captcha"):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )


@pytest.mark.asyncio
async def test_response_body_is_rejected_before_or_during_unbounded_download(
    tmp_path: Path,
) -> None:
    def declared_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000"}, content=b"small")

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        max_response_bytes=10,
        transport=httpx.MockTransport(declared_large),
    ) as client:
        with pytest.raises(ValueError, match="byte bound"):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )

    def streamed_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 11)

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        max_response_bytes=10,
        transport=httpx.MockTransport(streamed_large),
    ) as client:
        with pytest.raises(ValueError, match="byte bound"):
            await client.get(
                "https://www.inven.co.kr/board/maple/2304",
                scope=CrawlScope.APPROVED_SAMPLE,
            )


@pytest.mark.asyncio
async def test_comment_listing_uses_only_the_read_only_post_contract(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/common/board/comment.json.php"
        body = request.content.decode()
        assert "act=list" in body
        assert "comeidx=2304" in body
        assert "articlecode=123" in body
        assert "titles=100" in body
        return httpx.Response(200, json={"cmtcount": 0, "commentlist": []})

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.post_comment_list(
            board_id=2304,
            article_id=123,
            check_code="abcdefgh",
            scope=CrawlScope.APPROVED_SAMPLE,
            offset=100,
        )
    assert result.status_code == 200

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="outside"):
            await client.post_comment_list(
                board_id=9999,
                article_id=123,
                check_code="abcdefgh",
                scope=CrawlScope.APPROVED_SAMPLE,
            )


@pytest.mark.asyncio
async def test_comment_listing_allows_missing_optional_check_code(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "act=list" in body
        assert "chkcode=" not in body
        return httpx.Response(200, json={"cmtcount": 0, "commentlist": []})

    async with RespectfulInvenClient(
        settings(tmp_path),
        minimum_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await client.post_comment_list(
            board_id=2304,
            article_id=123,
            check_code=None,
            scope=CrawlScope.APPROVED_SAMPLE,
        )

    assert result.status_code == 200
