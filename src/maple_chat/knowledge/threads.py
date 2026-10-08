"""Bounded community graph projections over the existing article/comment foreign keys.

These are source relationships, not curated game facts. Reading the projection
requires neither a second copy of the corpus nor an embedding rebuild.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Article, Comment, SourceStatus
from maple_chat.retrieval.hybrid import Candidate

_SOURCE_KEY = re.compile(
    r"(?P<comment>comment:)?article:(?P<board>[0-9]+):(?P<article>[0-9]+)"
    r"(?::(?P<reply>[0-9]+))?"
)
_VISIBLE = (SourceStatus.SANITIZED, SourceStatus.INDEXED)


@dataclass(frozen=True, slots=True)
class ThreadNode:
    source_key: str
    kind: str
    text: str
    status: str
    content_hash: str
    published_at: str | None
    text_truncated: bool


@dataclass(frozen=True, slots=True)
class ThreadEdge:
    subject: str
    predicate: str
    object: str


@dataclass(frozen=True, slots=True)
class ThreadGraph:
    article_source_key: str
    title: str
    url: str
    nodes: tuple[ThreadNode, ...]
    edges: tuple[ThreadEdge, ...]
    incomplete: bool
    source_authority: str = "community"
    context_only: bool = True


def project_thread(
    article: Article,
    comments: tuple[Comment, ...],
    *,
    text_limit: int = 800,
    incomplete: bool = False,
) -> ThreadGraph:
    """Preserve containment; omit dangling, self-referencing and cyclic reply edges."""
    if not 1 <= text_limit <= 2000:
        raise ValueError("thread text limit must be between 1 and 2000")
    if article.status not in _VISIBLE:
        raise ValueError("article is not available for community graph projection")
    if any(comment.article_id != article.id for comment in comments):
        raise ValueError("thread comments must belong to the same article")
    key = f"article:{article.board_id}:{article.remote_article_id}"
    visible = {c.remote_comment_id: c for c in comments if c.status in _VISIBLE}

    def node(source_key: str, source: Article | Comment, kind: str) -> ThreadNode:
        return ThreadNode(
            source_key=source_key,
            kind=kind,
            text=source.sanitized_body[:text_limit],
            status=source.status.value,
            content_hash=source.content_hash,
            published_at=source.published_at.isoformat() if source.published_at else None,
            text_truncated=len(source.sanitized_body) > text_limit,
        )

    nodes = [node(key, article, "article")]
    edges: list[ThreadEdge] = []
    for remote_id, comment in sorted(visible.items()):
        comment_key = f"comment:{key}:{remote_id}"
        nodes.append(node(comment_key, comment, "comment"))
        edges.append(ThreadEdge(key, "has_comment", comment_key))
        parent = comment.parent_remote_comment_id
        if parent is None:
            continue
        visited = {remote_id}
        cursor: int | None = parent
        cyclic = False
        while cursor in visible:
            if cursor in visited:
                cyclic = True
                break
            visited.add(cursor)
            cursor = visible[cursor].parent_remote_comment_id
        if cyclic or parent not in visible:
            incomplete = True
            continue
        edges.append(ThreadEdge(comment_key, "reply_to", f"comment:{key}:{parent}"))
    return ThreadGraph(key, article.title, article.url, tuple(nodes), tuple(edges), incomplete)


async def load_thread_graph(
    session: AsyncSession,
    *,
    board_id: int,
    remote_article_id: int,
    remote_comment_id: int | None = None,
    max_comments: int = 32,
    max_hops: int = 4,
    text_limit: int = 800,
    indexed_only: bool = False,
) -> ThreadGraph | None:
    """Read one thread, or a selected comment and its bounded ancestor chain."""
    if not 0 <= max_comments <= 100 or not 0 <= max_hops <= 8:
        raise ValueError("thread limits require 0..100 comments and 0..8 ancestor hops")
    if not 1 <= text_limit <= 2000:
        raise ValueError("thread text limit must be between 1 and 2000")
    statuses = (SourceStatus.INDEXED,) if indexed_only else _VISIBLE
    article = await session.scalar(
        sa.select(Article).where(
            Article.board_id == board_id,
            Article.board_id > 0,
            Article.remote_article_id == remote_article_id,
            Article.status.in_(statuses),
        )
    )
    if article is None:
        return None
    query = sa.select(Comment).where(Comment.article_id == article.id, Comment.status.in_(statuses))
    if remote_comment_id is not None:
        query = query.where(Comment.remote_comment_id == remote_comment_id)
        limit = 1
    else:
        limit = max_comments
    rows = list(await session.scalars(query.order_by(Comment.remote_comment_id).limit(limit + 1)))
    incomplete = len(rows) > limit
    selected = {c.remote_comment_id: c for c in rows[:limit]}
    if remote_comment_id is not None and not selected:
        return None
    for _ in range(max_hops if remote_comment_id is not None else 0):
        parents = {
            c.parent_remote_comment_id
            for c in selected.values()
            if c.parent_remote_comment_id is not None and c.parent_remote_comment_id not in selected
        }
        if not parents:
            break
        ancestors = list(
            await session.scalars(
                sa.select(Comment).where(
                    Comment.article_id == article.id,
                    Comment.remote_comment_id.in_(parents),
                    Comment.status.in_(statuses),
                )
            )
        )
        if not ancestors:
            break
        selected.update((c.remote_comment_id, c) for c in ancestors)
    return project_thread(
        article, tuple(selected.values()), text_limit=text_limit, incomplete=incomplete
    )


async def attach_thread_context(
    factory: async_sessionmaker[AsyncSession], evidence: tuple[Candidate, ...]
) -> tuple[Candidate, ...]:
    """Attach community context without changing retrieval scores or evidence roles.

    Only indexed sources are exposed to generation. Each graph is bounded to one
    article and at most five comments; the selected comment keeps its original
    claim text. Other nodes explain references, not independent verified claims.
    """
    enriched: list[Candidate] = []
    if not evidence:
        return evidence
    async with factory() as session, session.begin():
        for candidate in evidence:
            match = _SOURCE_KEY.fullmatch(candidate.source_key)
            if match is None or bool(match["comment"]) != bool(match["reply"]):
                enriched.append(candidate)
                continue
            graph = await load_thread_graph(
                session,
                board_id=int(match["board"]),
                remote_article_id=int(match["article"]),
                remote_comment_id=int(match["reply"]) if match["reply"] else None,
                max_comments=3,
                text_limit=400,
                indexed_only=True,
            )
            enriched.append(
                replace(candidate, metadata={**candidate.metadata, "thread_graph": asdict(graph)})
                if graph is not None
                else candidate
            )
    return tuple(enriched)
