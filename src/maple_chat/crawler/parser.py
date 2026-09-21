"""Fixture-locked Inven listing, article, comment, and media parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from maple_chat.crawler.sanitizer import content_hash, hash_nickname, sanitize_text
from maple_chat.time import KST, require_aware_utc

PARSER_VERSION = "g003-v1"
_ARTICLE_PATH = re.compile(r"/board/maple/(?P<board>\d+)/(?P<article>\d+)(?:[/?#]|$)")
_COMMENT_CHECK_CODE = re.compile(
    r"(?:chkcode|cmtChkCode|commentChkCode)[\"']?\s*[:=]\s*[\"'](?P<value>[a-zA-Z0-9_-]{8,128})",
    re.IGNORECASE,
)


class ParserDriftError(RuntimeError):
    """Required source structure disappeared or became internally inconsistent."""


class ArticleUnavailable(RuntimeError):
    """A listed article now has no readable source body."""


@dataclass(frozen=True, slots=True)
class ListingArticle:
    board_id: int
    remote_article_id: int
    title: str
    url: str
    category: str | None
    published_at: datetime | None
    pinned: bool


@dataclass(frozen=True, slots=True)
class ListingPage:
    board_id: int
    categories: tuple[str, ...]
    articles: tuple[ListingArticle, ...]
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True, slots=True)
class MediaReference:
    kind: str
    url: str
    title: str | None
    alt_text: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    title: str
    sanitized_body: str
    author_hash: str | None
    view_count: int
    recommendation_count: int
    media: tuple[MediaReference, ...]
    masked_fields: frozenset[str]
    content_hash: str
    parser_version: str = PARSER_VERSION


@dataclass(frozen=True, slots=True)
class ParsedComment:
    remote_comment_id: int
    parent_remote_comment_id: int | None
    author_hash: str | None
    sanitized_body: str
    published_at: datetime | None
    score: int
    deleted: bool
    content_hash: str
    children: tuple[ParsedComment, ...] = field(default_factory=tuple)


def _attributes(raw: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name: value or "" for name, value in raw}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return require_aware_utc(parsed)
    except ValueError as exc:
        raise ParserDriftError("invalid source timestamp") from exc


class _ListingHTMLParser(HTMLParser):
    def __init__(self, board_id: int, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.board_id = board_id
        self.base_url = base_url
        self.categories: list[str] = []
        self.articles: list[ListingArticle] = []
        self._category = False
        self._category_text: list[str] = []
        self._article: dict[str, str] | None = None
        self._title = False
        self._title_text: list[str] = []

    def _append_article(self, values: dict[str, str], title: str) -> None:
        try:
            remote_id = int(values["data-article-id"])
        except (KeyError, ValueError) as exc:
            raise ParserDriftError("listing article id is invalid") from exc
        href = values.get("href", "")
        if not title or not href:
            raise ParserDriftError("listing article is missing title or URL")
        self.articles.append(
            ListingArticle(
                board_id=self.board_id,
                remote_article_id=remote_id,
                title=title,
                url=urljoin(self.base_url, href),
                category=values.get("data-category") or None,
                published_at=_parse_datetime(values.get("data-published-at")),
                pinned=values.get("data-pinned", "false").lower() == "true",
            )
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = _attributes(attrs)
        classes = values.get("class", "").split()
        href = values.get("href", "")
        if tag in {"a", "option"} and (
            "data-category" in values or ("category=" in href and not _ARTICLE_PATH.search(href))
        ):
            self._category = True
            self._category_text = []
        if tag == "article" and "data-article-id" in values:
            self._article = values
        if tag == "a" and "subject-link" in classes:
            match = _ARTICLE_PATH.search(href)
            if match is not None and int(match.group("board")) == self.board_id:
                self._article = {
                    "data-article-id": match.group("article"),
                    "href": href,
                    "data-category": values.get("data-category", ""),
                    "_append-on-anchor": "true",
                }
                self._title = True
                self._title_text = []
        elif self._article is not None and tag == "a" and "title" in classes:
            self._title = True
            self._title_text = []
            self._article["href"] = values.get("href", "")

    def handle_data(self, data: str) -> None:
        if self._category:
            self._category_text.append(data)
        if self._title:
            self._title_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "option"} and self._category:
            value = sanitize_text("".join(self._category_text)).text
            if value and value not in self.categories:
                self.categories.append(value)
            self._category = False
        if tag == "a" and self._title:
            if self._article is not None and self._article.get("_append-on-anchor") == "true":
                self._append_article(
                    self._article,
                    sanitize_text("".join(self._title_text)).text,
                )
                self._article = None
                self._title_text = []
            self._title = False
        if tag == "article" and self._article is not None:
            values = self._article
            title = sanitize_text("".join(self._title_text)).text
            self._append_article(values, title)
            self._article = None
            self._title_text = []


class _ArticleHTMLParser(HTMLParser):
    _BLOCK_TAGS = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "blockquote"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.author = ""
        self.view_count = 0
        self.recommendation_count = 0
        self.body: list[str] = []
        self.media: list[MediaReference] = []
        self._content_depth = 0
        self._content_seen = False
        self._content_tag: str | None = None
        self._capture_title = False
        self._capture_author = False
        self._capture_link_title = False
        self._link: dict[str, str] | None = None
        self._text: list[str] = []
        self._article_hit_depth = 0
        self._article_hit_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = _attributes(attrs)
        classes = values.get("class", "").split()
        if self._article_hit_depth:
            if tag == "div":
                self._article_hit_depth += 1
        elif tag == "div" and "articleHit" in classes:
            self._article_hit_depth = 1
            self._article_hit_text = []
        if values.get("id") == "powerbbsContent":
            self._content_seen = True
            self._content_depth = 1
            self._content_tag = tag
            return
        if self._content_depth:
            if tag == self._content_tag:
                self._content_depth += 1
            if tag in self._BLOCK_TAGS:
                self.body.append("\n")
            if tag == "img" and values.get("src"):
                self.media.append(
                    MediaReference(
                        kind="image",
                        url=urljoin(self.base_url, values["src"]),
                        title=None,
                        alt_text=sanitize_text(values.get("alt", "")).text or None,
                    )
                )
            if tag == "a" and values.get("href"):
                self._link = values
                self._capture_link_title = True
                self._text = []
        elif tag in {"h1", "meta"} and (
            values.get("data-title") == "article"
            or values.get("property") == "og:title"
            or (tag == "h1" and "article-title" in classes)
        ):
            if tag == "meta":
                self.title = sanitize_text(values.get("content", "")).text
            else:
                self._capture_title = True
                self._text = []
        elif values.get("data-author") == "nickname" or any(
            value in classes for value in ("user-name", "nickname", "article-author")
        ):
            self._capture_author = True
            self._text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._article_hit_depth:
            self._article_hit_text.append(data)
        if self._capture_title or self._capture_author or self._capture_link_title:
            self._text.append(data)
        if self._content_depth:
            self.body.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._article_hit_depth and tag == "div":
            self._article_hit_depth -= 1
            if self._article_hit_depth == 0:
                hit_text = " ".join(self._article_hit_text)
                view_match = re.search(r"조회\s*:\s*([\d,]+)", hit_text)
                recommendation_match = re.search(r"추천\s*:\s*([\d,]+)", hit_text)
                if view_match is not None:
                    self.view_count = int(view_match.group(1).replace(",", ""))
                if recommendation_match is not None:
                    self.recommendation_count = int(recommendation_match.group(1).replace(",", ""))
        if self._capture_title and tag == "h1":
            self.title = sanitize_text("".join(self._text)).text
            self._capture_title = False
            self._text = []
        if self._capture_author and tag in {"span", "div"}:
            self.author = "".join(self._text).strip()
            self._capture_author = False
            self._text = []
        if self._capture_link_title and tag == "a" and self._link is not None:
            title = sanitize_text("".join(self._text)).text or None
            kind = self._link.get("data-media", "external")
            self.media.append(
                MediaReference(
                    kind="video" if kind == "video" else "external",
                    url=urljoin(self.base_url, self._link["href"]),
                    title=title,
                )
            )
            self._capture_link_title = False
            self._link = None
            self._text = []
        if self._content_depth and tag == self._content_tag:
            self._content_depth -= 1
            if tag in self._BLOCK_TAGS:
                self.body.append("\n")
            if self._content_depth == 0:
                self._content_tag = None


def parse_listing(html: str, *, board_id: int, base_url: str) -> ListingPage:
    if board_id not in {2294, 2295, 2296, 2297, 2298, 2300, 2304}:
        raise ParserDriftError("unexpected board id")
    parser = _ListingHTMLParser(board_id, base_url)
    parser.feed(html)
    if not parser.categories:
        raise ParserDriftError("listing selectors produced no categories")
    unique = {article.remote_article_id: article for article in parser.articles}
    return ListingPage(board_id, tuple(parser.categories), tuple(unique.values()))


def parse_article(html: str, *, base_url: str, nickname_salt: str) -> ParsedArticle:
    parser = _ArticleHTMLParser(base_url)
    parser.feed(html)
    if parser._content_depth:
        raise ParserDriftError("#powerbbsContent is not closed")
    sanitized = sanitize_text("".join(parser.body))
    if not parser.title or not parser._content_seen:
        raise ParserDriftError("article title or #powerbbsContent is missing")
    if not sanitized.text:
        raise ArticleUnavailable("article has no searchable text")
    author_hash = hash_nickname(parser.author, nickname_salt) if parser.author else None
    return ParsedArticle(
        title=parser.title,
        sanitized_body=sanitized.text,
        author_hash=author_hash,
        view_count=parser.view_count,
        recommendation_count=parser.recommendation_count,
        media=tuple(parser.media),
        masked_fields=sanitized.masked_fields,
        content_hash=content_hash(parser.title, sanitized.text),
    )


def _normalized_comment_rows(payload: object) -> list[object]:
    if not isinstance(payload, dict):
        raise ParserDriftError("comment response schema is invalid")
    fixture_rows = payload.get("comments")
    if isinstance(fixture_rows, list):
        return fixture_rows
    groups = payload.get("commentlist")
    if not isinstance(groups, list):
        raise ParserDriftError("comment response schema is invalid")
    normalized: list[object] = []
    for group in groups:
        raw_rows = group.get("list") if isinstance(group, dict) else None
        if not isinstance(raw_rows, list):
            raise ParserDriftError("comment group schema is invalid")
        inven_ids: set[int] = set()
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise ParserDriftError("comment row is invalid")
            attributes = raw.get("__attr__", {})
            if not isinstance(attributes, dict):
                attributes = {}
            candidate = raw.get("id") or raw.get("cmtidx") or attributes.get("cmtidx")
            if candidate is not None and int(candidate) > 0:
                inven_ids.add(int(candidate))

        root_id: int | None = None
        for raw in raw_rows:
            attributes = raw.get("__attr__", {})
            if not isinstance(attributes, dict):
                attributes = {}
            comment_id_value = raw.get("id") or raw.get("cmtidx") or attributes.get("cmtidx")
            if comment_id_value is None:
                raise ParserDriftError("comment required field is invalid")
            comment_id = int(comment_id_value)
            if comment_id == 0:
                continue
            generic_parent = (
                raw.get("parent_id") or raw.get("parentcmtidx") or attributes.get("parentcmtidx")
            )
            inven_parent = raw.get("cmtpidx") or attributes.get("cmtpidx")
            if generic_parent is not None:
                parent_id: int | None = int(generic_parent)
            elif inven_parent is not None:
                parent_value = int(inven_parent)
                parent_id = (
                    None
                    if parent_value in {0, comment_id} or parent_value not in inven_ids
                    else parent_value
                )
            else:
                parent_id = root_id
            if root_id is None:
                root_id = comment_id
            body = raw.get("body")
            if body is None:
                body = raw.get("o_comment", raw.get("comment", raw.get("contents", "")))
            published = raw.get("published_at") or raw.get("o_datetime") or raw.get("o_date")
            if not published:
                published = "1970-01-01T00:00:00+00:00"
            state = raw.get("state", attributes.get("state"))
            normalized.append(
                {
                    "id": comment_id,
                    "parent_id": parent_id,
                    "author": raw.get("author", raw.get("o_name", "")),
                    "body": body,
                    "published_at": published,
                    "score": raw.get("score", raw.get("o_recommend", raw.get("o_good", 0))),
                    "deleted": bool(raw.get("deleted", False))
                    or str(raw.get("del_state", "0")) not in {"", "0", "false", "False"}
                    or (state is not None and str(state).upper() != "Y"),
                }
            )
    return normalized


def parse_comments(raw_json: str, *, nickname_salt: str) -> tuple[ParsedComment, ...]:
    try:
        payload = json.loads(raw_json)
        rows = _normalized_comment_rows(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ParserDriftError("comment response schema is invalid") from exc
    if not isinstance(rows, list):
        raise ParserDriftError("comment collection is not a list")

    flat: dict[int, ParsedComment] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ParserDriftError("comment row is invalid")
        try:
            comment_id = int(row["id"])
            parent_id = int(row["parent_id"]) if row.get("parent_id") is not None else None
            deleted = bool(row.get("deleted", False))
            body = "" if deleted else sanitize_text(str(row["body"])).text
            nickname = str(row.get("author", "")).strip()
            flat[comment_id] = ParsedComment(
                remote_comment_id=comment_id,
                parent_remote_comment_id=parent_id,
                author_hash=hash_nickname(nickname, nickname_salt) if nickname else None,
                sanitized_body=body,
                published_at=_parse_datetime(str(row["published_at"])),
                score=int(row.get("score", 0)),
                deleted=deleted,
                content_hash=content_hash(body, str(parent_id or "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ParserDriftError("comment required field is invalid") from exc

    children: dict[int, list[ParsedComment]] = {comment_id: [] for comment_id in flat}
    roots: list[ParsedComment] = []
    for comment in flat.values():
        parent_id = comment.parent_remote_comment_id
        if parent_id is None:
            roots.append(comment)
        elif parent_id == comment.remote_comment_id or parent_id not in flat:
            raise ParserDriftError("comment tree contains an orphan or cycle")
        else:
            children[parent_id].append(comment)

    def attach(comment: ParsedComment, lineage: frozenset[int]) -> ParsedComment:
        if comment.remote_comment_id in lineage:
            raise ParserDriftError("comment tree contains a cycle")
        next_lineage = lineage | {comment.remote_comment_id}
        return ParsedComment(
            remote_comment_id=comment.remote_comment_id,
            parent_remote_comment_id=comment.parent_remote_comment_id,
            author_hash=comment.author_hash,
            sanitized_body=comment.sanitized_body,
            published_at=comment.published_at,
            score=comment.score,
            deleted=comment.deleted,
            content_hash=comment.content_hash,
            children=tuple(
                attach(child, next_lineage) for child in children[comment.remote_comment_id]
            ),
        )

    attached = tuple(attach(root, frozenset()) for root in roots)

    def count_tree(comment: ParsedComment) -> int:
        return 1 + sum(count_tree(child) for child in comment.children)

    if sum(count_tree(root) for root in attached) != len(flat):
        raise ParserDriftError("comment tree contains a disconnected cycle")
    return attached


def extract_comment_check_code(article_html: str) -> str | None:
    """Extract the public read-only comment request token embedded in an article page."""
    match = _COMMENT_CHECK_CODE.search(article_html)
    return match.group("value") if match is not None else None


def is_recursive_fetch_forbidden(reference: MediaReference) -> bool:
    parsed = urlparse(reference.url)
    if reference.kind in {"video", "external"} or parsed.scheme not in {"http", "https"}:
        return True
    if reference.kind != "image":
        return False
    if parsed.hostname == "static.inven.co.kr":
        return not parsed.path.startswith("/image_2011/")
    return parsed.hostname not in {
        "inven.co.kr",
        "www.inven.co.kr",
        "upload.inven.co.kr",
        "upload2.inven.co.kr",
        "upload3.inven.co.kr",
    } or not parsed.path.startswith("/upload/")
