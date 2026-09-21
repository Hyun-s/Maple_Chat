from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maple_chat.db.models import SourceType
from maple_chat.indexing.chunking import (
    SourceDocument,
    chunk_document,
    comment_branch_document,
)


def document(text: str) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.ARTICLE,
        source_key="article:2304:1",
        title="사냥 팁",
        board="팁과 노하우",
        category="사냥",
        published_at=datetime(2026, 8, 4, tzinfo=UTC),
        text=text,
        metadata={"recommendation_count": 10},
    )


def test_chunking_is_deterministic_bounded_and_carries_header() -> None:
    text = "\n\n".join(f"## 구간 {index}\n" + "단어 " * 100 for index in range(12))
    first = chunk_document(document(text))
    second = chunk_document(document(text))
    assert first == second
    assert len(first) >= 2
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert all(chunk.token_count <= 700 for chunk in first)
    assert all("제목: 사냥 팁" in chunk.text for chunk in first)
    assert len({chunk.chunk_id for chunk in first}) == len(first)


def test_invalid_chunk_bounds_are_rejected() -> None:
    with pytest.raises(ValueError, match="bounds"):
        chunk_document(document("body"), target_tokens=100, max_tokens=80)


def test_comment_branch_keeps_question_context_and_reply_boundaries() -> None:
    branch = comment_branch_document(
        source_key="comment:2304:1:10",
        article_title="질문",
        article_context="원 질문 요약",
        board="질문과 답변",
        category="질문",
        published_at=None,
        messages=("첫 답변", "대댓글"),
        metadata={},
    )
    chunks = chunk_document(branch)
    assert len(chunks) == 1
    assert "질문 문맥: 원 질문 요약" in chunks[0].text
    assert "댓글 2: 대댓글" in chunks[0].text
    assert chunks[0].metadata["qa_branch"] is True


def test_overlap_is_dropped_when_the_next_section_needs_the_full_budget() -> None:
    text = "\n\n".join(("큰구간 " * 430, "겹침 " * 70, "다음구간 " * 650))

    chunks = chunk_document(document(text))

    assert len(chunks) >= 2
    assert all(chunk.token_count <= 700 for chunk in chunks)


def test_single_unbroken_punctuation_run_is_split_to_the_token_limit() -> None:
    chunks = chunk_document(document("!" * 1600))

    assert len(chunks) >= 2
    assert all(chunk.token_count <= 700 for chunk in chunks)
