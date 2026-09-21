"""Respectful allowlisted HTTP client with delay, redirect, retry, and circuit controls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from maple_chat.config import Settings
from maple_chat.crawler.circuit import CrawlCircuit, CrawlCircuitOpen, retry_after_seconds
from maple_chat.crawler.policy import CrawlScope, require_live_crawl


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status_code: int
    content: bytes
    content_type: str | None


class RespectfulInvenClient:
    def __init__(
        self,
        settings: Settings,
        *,
        minimum_delay_seconds: float = 1.0,
        max_response_bytes: int = 8 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.crawler_user_agent is None:
            raise ValueError("crawler identity is required")
        self.settings = settings
        self.minimum_delay_seconds = minimum_delay_seconds
        self.max_response_bytes = max_response_bytes
        self.circuit = CrawlCircuit()
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        headers = {
            "User-Agent": settings.crawler_user_agent,
            "Accept": "text/html,application/json,image/*;q=0.8",
        }
        if settings.crawler_contact is not None:
            headers["From"] = settings.crawler_contact
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(20.0),
            headers=headers,
            transport=transport,
        )

    async def __aenter__(self) -> RespectfulInvenClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self) -> None:
        loop = asyncio.get_running_loop()
        remaining = self.minimum_delay_seconds - (loop.time() - self._last_request_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_at = loop.time()

    async def get(self, url: str, *, scope: CrawlScope) -> FetchResult:
        return await self._request("GET", url, scope=scope)

    async def post_comment_list(
        self,
        *,
        board_id: int,
        article_id: int,
        check_code: str | None,
        scope: CrawlScope,
        offset: int = 0,
    ) -> FetchResult:
        if str(board_id) not in {"2294", "2295", "2296", "2297", "2298", "2300", "2304"}:
            raise ValueError("comment board is outside the approved scope")
        if article_id < 1 or offset < 0:
            raise ValueError("comment request parameters are invalid")
        if check_code is not None and (len(check_code) < 8 or not check_code.isascii()):
            raise ValueError("comment request parameters are invalid")
        form = {
            "comeidx": str(board_id),
            "articlecode": str(article_id),
            "sortorder": "date",
            "act": "list",
            "out": "json",
            "replynick": "",
            "replyidx": "0",
            "uploadurl": "",
            "imageposition": "",
            "videoloading": "lazy",
        }
        if check_code is not None:
            form["chkcode"] = check_code
        if offset:
            form["titles"] = str(offset)
        return await self._request(
            "POST",
            "https://www.inven.co.kr/common/board/comment.json.php",
            scope=scope,
            data=form,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        scope: CrawlScope,
        data: dict[str, str] | None = None,
    ) -> FetchResult:
        current = url
        async with self._lock:
            for _ in range(4):
                self.circuit.require_closed()
                require_live_crawl(self.settings, current, scope)
                await self._pace()
                async with self._client.stream(method, current, data=data) as response:
                    self.circuit.record_status(response.status_code)
                    if self.circuit.open:
                        raise CrawlCircuitOpen(f"crawler circuit is open: {self.circuit.reason}")
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise httpx.HTTPStatusError(
                                "redirect is missing Location",
                                request=response.request,
                                response=response,
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status_code == 429:
                        delay = retry_after_seconds(
                            response.headers.get("retry-after"), now=datetime.now(UTC)
                        )
                        if delay:
                            await asyncio.sleep(delay)
                        raise httpx.HTTPStatusError(
                            "rate limited",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    raw_length = response.headers.get("content-length")
                    if raw_length is not None:
                        try:
                            declared_length = int(raw_length)
                        except ValueError as exc:
                            raise ValueError("response Content-Length is invalid") from exc
                        if declared_length > self.max_response_bytes:
                            raise ValueError("response exceeds configured byte bound")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ValueError("response exceeds configured byte bound")
                    body = bytes(content)
                    lowered = body[:100_000].lower()
                    if b"captcha" in lowered or b"recaptcha" in lowered:
                        self.circuit.record_captcha()
                        raise CrawlCircuitOpen("crawler circuit is open: captcha")
                    return FetchResult(
                        str(response.url),
                        response.status_code,
                        body,
                        response.headers.get("content-type"),
                    )
        raise httpx.TooManyRedirects("redirect limit exceeded")
