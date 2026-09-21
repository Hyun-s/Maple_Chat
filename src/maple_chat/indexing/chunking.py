"""Structure-aware deterministic chunk construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from maple_chat.crawler.sanitizer import content_hash
from maple_chat.db.models import SourceType

_TOKEN = re.compile(r"[\w가-힣]+|[^\s\w]", re.UNICODE)
_BOUNDARY = re.compile(r"\n\s*\n|(?=^#{1,4}\s)|(?=^[-*]\s)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_type: SourceType
    source_key: str
    title: str
    board: str
    category: str | None
    published_at: datetime | None
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    chunk_id: str
    source_type: SourceType
    source_key: str
    ordinal: int
    text: str
    token_count: int
    metadata: dict[str, Any]
    chunking_revision: str


def token_count(text: str) -> int:
    return len(_TOKEN.findall(text))


def _header(document: SourceDocument) -> str:
    date = document.published_at.date().isoformat() if document.published_at else "날짜 미상"
    category = document.category or "분류 없음"
    return f"제목: {document.title}\n게시판: {document.board}\n분류: {category}\n작성일: {date}"


def _split_oversized(value: str, limit: int) -> list[str]:
    words = value.split()
    if not words:
        return []
    groups: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for word in words:
        word_tokens = max(1, token_count(word))
        if word_tokens > limit:
            if current:
                groups.append(" ".join(current))
                current = []
                current_tokens = 0
            groups.extend(word[index : index + limit] for index in range(0, len(word), limit))
            continue
        if current and current_tokens + word_tokens > limit:
            groups.append(" ".join(current))
            current = []
            current_tokens = 0
        current.append(word)
        current_tokens += word_tokens
    if current:
        groups.append(" ".join(current))
    return groups


def chunk_document(
    document: SourceDocument,
    *,
    revision: str = "structure-v1",
    target_tokens: int = 550,
    max_tokens: int = 700,
    overlap_tokens: int = 80,
) -> tuple[ChunkDraft, ...]:
    if not (0 <= overlap_tokens < target_tokens <= max_tokens):
        raise ValueError("invalid chunk token bounds")
    header = _header(document)
    header_tokens = token_count(header)
    body_limit = max_tokens - header_tokens
    if body_limit <= overlap_tokens:
        raise ValueError("chunk header exceeds configured token budget")
    sections: list[str] = []
    for section in _BOUNDARY.split(document.text):
        cleaned = section.strip()
        if not cleaned:
            continue
        sections.extend(_split_oversized(cleaned, body_limit))

    bodies: list[str] = []
    current: list[str] = []
    current_count = 0
    target_body_tokens = max(1, target_tokens - header_tokens)
    for section in sections:
        section_count = token_count(section)
        if current and current_count + section_count > target_body_tokens:
            bodies.append("\n\n".join(current))
            overlap: list[str] = []
            overlap_count = 0
            for prior in reversed(current):
                if overlap_count + token_count(prior) > overlap_tokens:
                    break
                overlap.insert(0, prior)
                overlap_count += token_count(prior)
            if overlap_count + section_count > body_limit:
                overlap = []
                overlap_count = 0
            current = overlap
            current_count = overlap_count
        current.append(section)
        current_count += section_count
    if current:
        bodies.append("\n\n".join(current))

    drafts: list[ChunkDraft] = []
    for ordinal, body in enumerate(bodies):
        text = f"{header}\n\n{body}"
        count = token_count(text)
        if count > max_tokens:
            raise ValueError("chunk exceeded maximum token count")
        chunk_id = content_hash(document.source_key, str(ordinal), revision, text)
        metadata = dict(document.metadata)
        metadata.update(
            {
                "title": document.title,
                "board": document.board,
                "category": document.category,
                "published_at": document.published_at.isoformat()
                if document.published_at
                else None,
            }
        )
        drafts.append(
            ChunkDraft(
                chunk_id=chunk_id,
                source_type=document.source_type,
                source_key=document.source_key,
                ordinal=ordinal,
                text=text,
                token_count=count,
                metadata=metadata,
                chunking_revision=revision,
            )
        )
    return tuple(drafts)


def comment_branch_document(
    *,
    source_key: str,
    article_title: str,
    article_context: str,
    board: str,
    category: str | None,
    published_at: datetime | None,
    messages: tuple[str, ...],
    metadata: dict[str, Any],
) -> SourceDocument:
    conversation = "\n".join(
        f"댓글 {index + 1}: {message}" for index, message in enumerate(messages)
    )
    return SourceDocument(
        source_type=SourceType.COMMENT,
        source_key=source_key,
        title=article_title,
        board=board,
        category=category,
        published_at=published_at,
        text=f"질문 문맥: {article_context.strip()}\n\n{conversation}",
        metadata={**metadata, "qa_branch": True},
    )
