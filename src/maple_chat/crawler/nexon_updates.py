"""Official MapleStory update-note parsing, fetching, and incremental persistence."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.crawler.sanitizer import content_hash, sanitize_text
from maple_chat.db.models import Article, Board, Chunk, ChunkEmbedding, SourceStatus
from maple_chat.db.repositories import ArticleRecord, article_source_key, upsert_article_with_outbox
from maple_chat.time import KST, require_aware_utc

PATCH_NOTE_BOARD_ID = -1001
PATCH_NOTE_BOARD_NAME = "메이플스토리 공식 패치노트"
PATCH_NOTE_CATEGORY = "업데이트"
NEXON_UPDATE_ROOT = "https://maplestory.nexon.com/News/Update"
_UPDATE_PATH = re.compile(r"^/news/update(?:/(?P<article_id>\d+))?/?$", re.IGNORECASE)


class PatchNoteParserDrift(RuntimeError):
    """The official update page no longer matches the bounded parser contract."""


@dataclass(frozen=True, slots=True)
class PatchNoteListing:
    remote_article_id: int
    title: str
    url: str
    published_at: datetime
    source_modified_at: datetime | None


@dataclass(frozen=True, slots=True)
class PatchNoteSyncResult:
    list_pages: int
    discovered: int
    details_fetched: int
    inserted: int
    updated: int
    unchanged: int
    expired: int


def _attrs(raw: list[tuple[str, str | None]]) -> dict[str, str]:
    return {key: value or "" for key, value in raw}


def _kst_datetime(value: str, formats: tuple[str, ...]) -> datetime:
    for format_ in formats:
        try:
            parsed = datetime.strptime(value.strip(), format_)
            return parsed.replace(tzinfo=KST).astimezone(UTC)
        except ValueError:
            continue
    raise PatchNoteParserDrift(f"invalid official update timestamp: {value!r}")


class _UpdateListingParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.entries: list[PatchNoteListing] = []
        self._li_depth = 0
        self._current: dict[str, object] | None = None
        self._capture_title = False
        self._capture_date = False
        self._title: list[str] = []
        self._date: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = _attrs(attrs)
        if tag == "li":
            self._li_depth += 1
            if self._li_depth == 1:
                self._current = {}
        if self._current is None:
            return
        if tag == "a":
            href = values.get("href", "")
            match = _UPDATE_PATH.match(urlparse(urljoin(self.base_url, href)).path)
            if match is not None and match.group("article_id"):
                self._current["remote_article_id"] = int(match.group("article_id"))
                self._current["url"] = urljoin(self.base_url, href).split("?", 1)[0]
                self._capture_title = True
                self._title = []
        elif tag == "em" and "modify_common" in values.get("class", "").split():
            modified = values.get("data-modifytime")
            if modified:
                self._current["source_modified_at"] = _kst_datetime(
                    modified, ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
                )
        elif tag == "dd":
            self._capture_date = True
            self._date = []

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title.append(data)
        if self._capture_date:
            self._date.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
            if self._current is not None:
                self._current["title"] = " ".join(sanitize_text(" ".join(self._title)).text.split())
        elif tag == "dd" and self._capture_date:
            self._capture_date = False
            if self._current is not None:
                date_text = sanitize_text("".join(self._date)).text
                if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", date_text):
                    self._current["published_at"] = _kst_datetime(date_text, ("%Y.%m.%d",))
        elif tag == "li" and self._li_depth:
            self._li_depth -= 1
            if self._li_depth == 0:
                self._finish_entry()

    def _finish_entry(self) -> None:
        current = self._current or {}
        self._current = None
        remote_article_id = current.get("remote_article_id")
        title = current.get("title")
        url = current.get("url")
        published_at = current.get("published_at")
        source_modified_at = current.get("source_modified_at")
        if (
            not isinstance(remote_article_id, int)
            or not isinstance(title, str)
            or not isinstance(url, str)
            or not isinstance(published_at, datetime)
            or (source_modified_at is not None and not isinstance(source_modified_at, datetime))
        ):
            return
        self.entries.append(
            PatchNoteListing(
                remote_article_id=remote_article_id,
                title=title,
                url=url,
                published_at=published_at,
                source_modified_at=source_modified_at,
            )
        )


class _UpdateBodyParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {"br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "ul", "ol"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.content_seen = False
        self._qs_depth = 0
        self._capture_depth: int | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = _attrs(attrs)
        classes = values.get("class", "").split()
        if tag == "div":
            if self._qs_depth:
                self._qs_depth += 1
            elif "qs_text" in classes:
                self._qs_depth = 1
            if self._qs_depth and self._capture_depth is None and "new_board_con" in classes:
                self._capture_depth = self._qs_depth
                self.content_seen = True
        if self._capture_depth is None:
            return
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.body.append("\n")
        if not self._ignored_depth and tag == "img":
            alt = sanitize_text(values.get("alt", "")).text
            if alt:
                self.body.append(f" [이미지: {alt}] ")

    def handle_data(self, data: str) -> None:
        if self._capture_depth is not None and not self._ignored_depth:
            self.body.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth is not None:
            if tag in {"script", "style", "noscript"} and self._ignored_depth:
                self._ignored_depth -= 1
            elif not self._ignored_depth and tag in self._BLOCK_TAGS:
                self.body.append("\n")
        if tag == "div" and self._qs_depth:
            if self._capture_depth == self._qs_depth:
                self._capture_depth = None
            self._qs_depth -= 1


def parse_patch_note_listing(
    html: str,
    *,
    base_url: str = NEXON_UPDATE_ROOT,
) -> tuple[PatchNoteListing, ...]:
    parser = _UpdateListingParser(base_url)
    parser.feed(html)
    unique = {entry.remote_article_id: entry for entry in parser.entries}
    if not unique:
        raise PatchNoteParserDrift("official update listing produced no entries")
    return tuple(unique.values())


def parse_patch_note_body(html: str) -> str:
    parser = _UpdateBodyParser()
    parser.feed(html)
    if parser._capture_depth is not None or parser._qs_depth:
        raise PatchNoteParserDrift("official update content container is not closed")
    body = sanitize_text("".join(parser.body)).text
    if not parser.content_seen or not body:
        raise PatchNoteParserDrift("official update content container is missing or empty")
    return body


def validate_nexon_update_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "maplestory.nexon.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("official update URL is outside the HTTPS Nexon allowlist")
    match = _UPDATE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("official update URL path is outside /News/Update")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"page"} or any(
        len(values) != 1 or not values[0].isdigit() or int(values[0]) < 1
        for values in query.values()
    ):
        raise ValueError("official update URL query is invalid")


class OfficialNexonClient:
    """GET-only Nexon client with a strict host/path boundary and bounded responses."""

    def __init__(
        self,
        user_agent: str,
        *,
        contact: str | None = None,
        minimum_delay_seconds: float = 1.0,
        max_response_bytes: int = 12 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if len(user_agent.strip()) < 8:
            raise ValueError("official collector user agent is required")
        if minimum_delay_seconds < 0 or max_response_bytes < 1:
            raise ValueError("official collector bounds are invalid")
        headers = {"User-Agent": user_agent, "Accept": "text/html"}
        if contact:
            headers["From"] = contact
        self.minimum_delay_seconds = minimum_delay_seconds
        self.max_response_bytes = max_response_bytes
        self._last_request_at = 0.0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(30.0),
            headers=headers,
            transport=transport,
        )

    async def __aenter__(self) -> OfficialNexonClient:
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

    async def get_html(self, url: str) -> str:
        current = url
        async with self._lock:
            for _ in range(4):
                validate_nexon_update_url(current)
                await self._pace()
                async with self._client.stream("GET", current) as response:
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
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if not content_type.casefold().startswith("text/html"):
                        raise ValueError("official update response is not HTML")
                    declared = response.headers.get("content-length")
                    if declared:
                        try:
                            declared_length = int(declared)
                        except ValueError as exc:
                            raise ValueError("official update Content-Length is invalid") from exc
                        if declared_length > self.max_response_bytes:
                            raise ValueError("official update response exceeds byte bound")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ValueError("official update response exceeds byte bound")
                    return bytes(content).decode("utf-8-sig")
        raise httpx.TooManyRedirects("official update redirect limit exceeded")


def _cutoff_at_kst_midnight(observed_at: datetime, since_days: int) -> datetime:
    if since_days < 1 or since_days > 3650:
        raise ValueError("patch-note retention must be between 1 and 3650 days")
    observed_kst = require_aware_utc(observed_at).astimezone(KST)
    cutoff_date = observed_kst.date() - timedelta(days=since_days)
    return datetime.combine(cutoff_date, time(), tzinfo=KST).astimezone(UTC)


async def sync_recent_patch_notes(
    session: AsyncSession,
    *,
    client: OfficialNexonClient,
    observed_at: datetime,
    since_days: int = 365,
    max_pages: int = 60,
) -> PatchNoteSyncResult:
    """Scan the rolling window, fetch only new/modified details, and expire old vectors."""

    observed_at = require_aware_utc(observed_at)
    if max_pages < 1 or max_pages > 200:
        raise ValueError("patch-note listing page bound must be between 1 and 200")
    cutoff = _cutoff_at_kst_midnight(observed_at, since_days)
    listings: dict[int, PatchNoteListing] = {}
    pages_read = 0
    for page in range(1, max_pages + 1):
        html = await client.get_html(f"{NEXON_UPDATE_ROOT}?page={page}")
        entries = parse_patch_note_listing(html)
        pages_read += 1
        for entry in entries:
            if entry.published_at >= cutoff:
                listings[entry.remote_article_id] = entry
        if min(entry.published_at for entry in entries) < cutoff:
            break
    else:
        raise RuntimeError("patch-note listing hit max_pages before reaching the cutoff")

    await session.execute(
        insert(Board)
        .values(
            board_id=PATCH_NOTE_BOARD_ID,
            name=PATCH_NOTE_BOARD_NAME,
            enabled=True,
            crawl_checkpoint={},
        )
        .on_conflict_do_update(
            index_elements=[Board.board_id],
            set_={"name": PATCH_NOTE_BOARD_NAME, "enabled": True},
        )
    )
    from maple_chat.db.repositories import upsert_category

    category_id = await upsert_category(
        session,
        board_id=PATCH_NOTE_BOARD_ID,
        remote_name=PATCH_NOTE_CATEGORY,
        observed_at=observed_at,
    )
    existing_rows = (
        await session.execute(
            sa.select(Article).where(
                Article.board_id == PATCH_NOTE_BOARD_ID,
                Article.remote_article_id.in_(list(listings)),
            )
        )
    ).scalars()
    existing = {article.remote_article_id: article for article in existing_rows}

    fetched = inserted_count = updated_count = unchanged_count = 0
    for listing in sorted(listings.values(), key=lambda entry: entry.remote_article_id):
        article = existing.get(listing.remote_article_id)
        unchanged = (
            article is not None
            and article.status in {SourceStatus.SANITIZED, SourceStatus.INDEXED}
            and article.title == listing.title
            and article.published_at == listing.published_at
            and (
                listing.source_modified_at is None
                or article.source_modified_at == listing.source_modified_at
            )
        )
        if article is not None and unchanged:
            article.last_observed_at = observed_at
            unchanged_count += 1
            continue

        body = parse_patch_note_body(await client.get_html(listing.url))
        fetched += 1
        saved, changed = await upsert_article_with_outbox(
            session,
            ArticleRecord(
                board_id=PATCH_NOTE_BOARD_ID,
                remote_article_id=listing.remote_article_id,
                category_id=category_id,
                url=listing.url,
                title=listing.title,
                sanitized_body=body,
                content_hash=content_hash(listing.title, body),
                observed_at=observed_at,
                published_at=listing.published_at,
                source_modified_at=listing.source_modified_at,
            ),
        )
        # A source revision timestamp can advance even when sanitization produces
        # the same content hash. Persist observation metadata and revive a source
        # if an operator deliberately widens the rolling window later.
        saved.url = listing.url
        saved.title = listing.title
        saved.published_at = listing.published_at
        saved.source_modified_at = listing.source_modified_at
        saved.status = SourceStatus.SANITIZED
        if article is None:
            inserted_count += 1
        elif changed:
            updated_count += 1
        else:
            unchanged_count += 1

    expired_articles = tuple(
        (
            await session.execute(
                sa.select(Article).where(
                    Article.board_id == PATCH_NOTE_BOARD_ID,
                    Article.published_at < cutoff,
                    Article.status.in_([SourceStatus.SANITIZED, SourceStatus.INDEXED]),
                )
            )
        ).scalars()
    )
    expired_keys = tuple(
        article_source_key(PATCH_NOTE_BOARD_ID, article.remote_article_id)
        for article in expired_articles
    )
    if expired_keys:
        chunk_ids = sa.select(Chunk.chunk_id).where(Chunk.source_key.in_(expired_keys))
        await session.execute(
            sa.update(ChunkEmbedding)
            .where(ChunkEmbedding.chunk_id.in_(chunk_ids))
            .values(active=False)
        )
        await session.execute(
            sa.update(Chunk).where(Chunk.source_key.in_(expired_keys)).values(active=False)
        )
        for article in expired_articles:
            article.status = SourceStatus.DELETED

    board = await session.get(Board, PATCH_NOTE_BOARD_ID, with_for_update=True)
    if board is None:
        raise RuntimeError("official patch-note board disappeared")
    board.crawl_checkpoint = {
        "last_success_at": observed_at.isoformat(),
        "since_days": since_days,
        "cutoff": cutoff.isoformat(),
        "latest_article_id": max(listings) if listings else None,
    }
    board.updated_at = observed_at
    await session.flush()
    return PatchNoteSyncResult(
        list_pages=pages_read,
        discovered=len(listings),
        details_fetched=fetched,
        inserted=inserted_count,
        updated=updated_count,
        unchanged=unchanged_count,
        expired=len(expired_articles),
    )
