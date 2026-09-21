"""Offline seven-board ingestion used until the external rights gate opens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.crawler.parser import ParsedComment, parse_article, parse_comments, parse_listing
from maple_chat.db.repositories import (
    ArticleRecord,
    CommentRecord,
    SourceDenied,
    article_source_key,
    upsert_article_with_outbox,
    upsert_category,
    upsert_comment_with_outbox,
)


@dataclass(frozen=True, slots=True)
class FixtureIngestResult:
    board_id: int
    categories: int
    articles: int
    comments: int


def _flatten_comments(comments: tuple[ParsedComment, ...]) -> tuple[ParsedComment, ...]:
    flattened: list[ParsedComment] = []

    def visit(comment: ParsedComment) -> None:
        flattened.append(comment)
        for child in comment.children:
            visit(child)

    for comment in comments:
        visit(comment)
    return tuple(flattened)


class FixtureCorpus:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _read(self, *parts: str) -> str:
        path = self.root.joinpath(*parts).resolve()
        if self.root not in path.parents:
            raise ValueError("fixture path escaped configured root")
        return path.read_text(encoding="utf-8")

    def listing(self, board_id: int) -> str:
        return self._read("listings", f"{board_id}.html")

    def article(self, board_id: int, remote_article_id: int) -> str:
        return self._read("articles", f"{board_id}-{remote_article_id}.html")

    def comments(self, board_id: int, remote_article_id: int) -> str | None:
        path = self.root / "comments" / f"{board_id}-{remote_article_id}.json"
        return path.read_text(encoding="utf-8") if path.is_file() else None


async def ingest_fixture_board(
    session: AsyncSession,
    *,
    corpus: FixtureCorpus,
    board_id: int,
    observed_at: datetime,
    nickname_salt: str,
) -> FixtureIngestResult:
    base_url = f"https://www.inven.co.kr/board/maple/{board_id}"
    listing = parse_listing(corpus.listing(board_id), board_id=board_id, base_url=base_url)
    categories = {
        name: await upsert_category(
            session,
            board_id=board_id,
            remote_name=name,
            observed_at=observed_at,
        )
        for name in listing.categories
    }
    article_count = 0
    comment_count = 0
    for item in listing.articles:
        parsed = parse_article(
            corpus.article(board_id, item.remote_article_id),
            base_url=item.url,
            nickname_salt=nickname_salt,
        )
        try:
            article, _ = await upsert_article_with_outbox(
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
                ),
            )
        except SourceDenied:
            continue
        article_count += 1
        raw_comments = corpus.comments(board_id, item.remote_article_id)
        if raw_comments is None:
            continue
        for comment in _flatten_comments(parse_comments(raw_comments, nickname_salt=nickname_salt)):
            await upsert_comment_with_outbox(
                session,
                article_id=article.id,
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
    return FixtureIngestResult(board_id, len(categories), article_count, comment_count)
