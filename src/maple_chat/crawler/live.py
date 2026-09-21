"""Rights-gated, bounded live Inven ingestion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.crawler.circuit import CrawlCircuitOpen
from maple_chat.crawler.http import FetchResult, RespectfulInvenClient
from maple_chat.crawler.media import (
    MediaRejected,
    OCRProvider,
    TesseractOCRProvider,
    inspect_image,
    run_bounded_ocr,
)
from maple_chat.crawler.parser import (
    ArticleUnavailable,
    ListingArticle,
    ParsedComment,
    ParserDriftError,
    extract_comment_check_code,
    is_recursive_fetch_forbidden,
    parse_article,
    parse_comments,
    parse_listing,
)
from maple_chat.crawler.policy import CrawlScope
from maple_chat.db.models import Article, MediaAsset
from maple_chat.db.repositories import (
    ArticleRecord,
    CommentRecord,
    SourceDenied,
    article_source_key,
    exclude_article,
    upsert_article_with_outbox,
    upsert_category,
    upsert_comment_with_outbox,
)
from maple_chat.scheduler.planner import (
    BOARD_NAMES,
    record_board_progress,
    record_board_refresh,
)


@dataclass(frozen=True, slots=True)
class LiveIngestResult:
    board_id: int
    page: int
    categories: int
    discovered_articles: int
    ingested_articles: int
    comments: int
    ocr_assets: int
    skipped_existing: int = 0
    unseen_articles: int = 0


def select_listing_articles(
    articles: tuple[ListingArticle, ...],
    *,
    max_articles: int,
    scope: CrawlScope,
) -> list[ListingArticle]:
    """Bound sample crawls while refusing silent truncation during a full backfill."""
    if scope is CrawlScope.FULL_BACKFILL and len(articles) > max_articles:
        raise ValueError(
            "full-backfill listing exceeds max_articles; increase the bound before continuing"
        )
    return list(articles[:max_articles])


def decode_response(result: FetchResult) -> str:
    content_type = result.content_type or ""
    declared = None
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.casefold() == "charset" and value:
            declared = value.strip(" \"'")
            break
    encodings = [value for value in (declared, "utf-8", "cp949") if value]
    candidates: list[tuple[int, int, str]] = []
    for priority, encoding in enumerate(dict.fromkeys(encodings)):
        try:
            decoded = result.content.decode(encoding, errors="replace")
        except LookupError:
            continue
        candidates.append((decoded.count("�"), priority, decoded))
    if not candidates:
        return result.content.decode("utf-8", errors="replace")
    return min(candidates)[2]


def _flatten_comments(comments: tuple[ParsedComment, ...]) -> tuple[ParsedComment, ...]:
    flattened: list[ParsedComment] = []

    def visit(comment: ParsedComment) -> None:
        flattened.append(comment)
        for child in comment.children:
            visit(child)

    for comment in comments:
        visit(comment)
    return tuple(flattened)


async def _load_comments(
    client: RespectfulInvenClient,
    *,
    board_id: int,
    article_id: int,
    check_code: str | None,
    nickname_salt: str,
    scope: CrawlScope,
) -> tuple[ParsedComment, ...]:
    groups: list[object] = []
    offset = 0
    total = 1
    while offset < total:
        response = await client.post_comment_list(
            board_id=board_id,
            article_id=article_id,
            check_code=check_code,
            scope=scope,
            offset=offset,
        )
        try:
            payload = json.loads(decode_response(response))
            if not isinstance(payload, dict):
                raise TypeError
            page_groups = payload.get("commentlist", [])
            if not isinstance(page_groups, list):
                raise TypeError
            groups.extend(page_groups)
            total = max(0, int(payload.get("cmtcount", 0)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ParserDriftError("comment response schema is invalid") from exc
        offset += 100
    if not groups:
        return ()
    return parse_comments(
        json.dumps({"commentlist": groups}, ensure_ascii=False),
        nickname_salt=nickname_salt,
    )


async def _store_media(
    session: AsyncSession,
    *,
    client: RespectfulInvenClient,
    article_id: int,
    image_url: str,
    scope: CrawlScope,
    ocr: OCRProvider,
) -> bool:
    url_hash = hashlib.sha256(image_url.encode()).hexdigest()
    values: dict[str, object] = {
        "article_id": article_id,
        "source_url": image_url,
        "source_url_hash": url_hash,
        "fetch_status": "rejected",
    }
    try:
        response = await client.get(image_url, scope=scope)
        mime_type = (response.content_type or "").partition(";")[0].strip().lower()
        metadata = inspect_image(response.content, mime_type)
        recognized, confidence = await run_bounded_ocr(ocr, response.content, metadata)
        values.update(
            {
                "mime_type": metadata.mime_type,
                "width": metadata.width,
                "height": metadata.height,
                "fetch_status": "ocr_ready" if recognized.text else "ocr_empty",
                "ocr_text": recognized.text or None,
                "ocr_confidence": confidence,
            }
        )
    except (MediaRejected, httpx.HTTPStatusError, httpx.TransportError, ValueError):
        pass
    except CrawlCircuitOpen:
        raise
    await session.execute(
        insert(MediaAsset)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[MediaAsset.article_id, MediaAsset.source_url_hash],
            set_={key: value for key, value in values.items() if key != "article_id"},
        )
    )
    return values["fetch_status"] == "ocr_ready"


async def ingest_live_board_page(
    session: AsyncSession,
    *,
    client: RespectfulInvenClient,
    board_id: int,
    page: int,
    observed_at: datetime,
    nickname_salt: str,
    scope: CrawlScope,
    max_articles: int = 30,
    include_comments: bool = True,
    include_ocr: bool = True,
    ocr: OCRProvider | None = None,
    preserve_backfill_checkpoint: bool = False,
    skip_existing: bool = False,
) -> LiveIngestResult:
    if board_id not in BOARD_NAMES:
        raise ValueError("board is outside the approved seven-board scope")
    if page < 1 or max_articles < 1 or max_articles > 100:
        raise ValueError("live crawl bounds are invalid")
    listing_url = f"https://www.inven.co.kr/board/maple/{board_id}?p={page}"
    try:
        listing_response = await client.get(listing_url, scope=scope)
        listing = parse_listing(
            decode_response(listing_response),
            board_id=board_id,
            base_url=listing_url,
        )
    except ParserDriftError:
        client.circuit.record_parser_drift()
        raise

    categories = {
        name: await upsert_category(
            session,
            board_id=board_id,
            remote_name=name,
            observed_at=observed_at,
        )
        for name in listing.categories
    }
    ingested = 0
    comment_count = 0
    ocr_count = 0
    ocr_provider = ocr or (TesseractOCRProvider() if include_ocr else None)
    selected = select_listing_articles(
        listing.articles,
        max_articles=max_articles,
        scope=scope,
    )
    observed_ids = [item.remote_article_id for item in selected]
    skipped_existing = 0
    unseen_articles = len(selected)
    if skip_existing and observed_ids:
        existing_ids = set(
            await session.scalars(
                sa.select(Article.remote_article_id).where(
                    Article.board_id == board_id,
                    Article.remote_article_id.in_(observed_ids),
                )
            )
        )
        skipped_existing = len(existing_ids)
        selected = [item for item in selected if item.remote_article_id not in existing_ids]
        unseen_articles = len(selected)
    for item in selected:
        try:
            article_response = await client.get(item.url, scope=scope)
            article_html = decode_response(article_response)
            parsed = parse_article(
                article_html,
                base_url=item.url,
                nickname_salt=nickname_salt,
            )
        except ArticleUnavailable:
            existing = await session.scalar(
                sa.select(Article)
                .where(
                    Article.board_id == board_id,
                    Article.remote_article_id == item.remote_article_id,
                )
                .with_for_update()
            )
            if existing is not None:
                await exclude_article(
                    session,
                    article=existing,
                    reason="source article is unavailable",
                    observed_at=observed_at,
                )
            continue
        except ParserDriftError:
            client.circuit.record_parser_drift()
            raise
        try:
            stored, _ = await upsert_article_with_outbox(
                session,
                ArticleRecord(
                    board_id=board_id,
                    remote_article_id=item.remote_article_id,
                    category_id=categories.get(item.category or ""),
                    url=item.url,
                    title=parsed.title,
                    sanitized_body=parsed.sanitized_body,
                    content_hash=parsed.content_hash,
                    observed_at=observed_at,
                    author_hash=parsed.author_hash,
                    published_at=item.published_at,
                    view_count=parsed.view_count,
                    recommendation_count=parsed.recommendation_count,
                ),
            )
        except SourceDenied:
            continue
        ingested += 1

        if include_comments:
            check_code = extract_comment_check_code(article_html)
            comments = await _load_comments(
                client,
                board_id=board_id,
                article_id=item.remote_article_id,
                check_code=check_code,
                nickname_salt=nickname_salt,
                scope=scope,
            )
            flattened_comments = _flatten_comments(comments)
            stored.comment_count = len(flattened_comments)
            for comment in flattened_comments:
                await upsert_comment_with_outbox(
                    session,
                    article_id=stored.id,
                    article_source=article_source_key(board_id, item.remote_article_id),
                    record=CommentRecord(
                        remote_comment_id=comment.remote_comment_id,
                        parent_remote_comment_id=comment.parent_remote_comment_id,
                        sanitized_body=comment.sanitized_body,
                        content_hash=comment.content_hash,
                        observed_at=observed_at,
                        author_hash=comment.author_hash,
                        published_at=comment.published_at,
                        score=comment.score,
                        deleted=comment.deleted,
                    ),
                )
                comment_count += 1

        if include_ocr and ocr_provider is not None:
            for reference in parsed.media:
                if reference.kind != "image" or is_recursive_fetch_forbidden(reference):
                    continue
                if await _store_media(
                    session,
                    client=client,
                    article_id=stored.id,
                    image_url=reference.url,
                    scope=scope,
                    ocr=ocr_provider,
                ):
                    ocr_count += 1

    if preserve_backfill_checkpoint:
        await record_board_refresh(
            session,
            board_id=board_id,
            succeeded_at=observed_at,
        )
    else:
        await record_board_progress(
            session,
            board_id=board_id,
            page=page,
            observed_article_ids=tuple(observed_ids),
            completed=not listing.articles,
            succeeded_at=observed_at,
        )
    return LiveIngestResult(
        board_id=board_id,
        page=page,
        categories=len(categories),
        discovered_articles=len(listing.articles),
        ingested_articles=ingested,
        comments=comment_count,
        ocr_assets=ocr_count,
        skipped_existing=skipped_existing,
        unseen_articles=unseen_articles,
    )
