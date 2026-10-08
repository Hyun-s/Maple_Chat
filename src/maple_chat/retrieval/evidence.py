"""Evidence roles and source-family boundaries shared by retrieval and QA."""

from __future__ import annotations

import re

from maple_chat.retrieval.hybrid import Candidate

_COMMENT_BOUNDARY = re.compile(r"(?:^|\n)(댓글 \d+:)")


def evidence_claim_role(candidate: Candidate) -> str:
    if candidate.metadata.get("qa_branch") is True:
        return "answer_with_question_context"
    if candidate.metadata.get("board") == "질문과 답변":
        return "question_only"
    return "community_post"


def claim_bearing_text(candidate: Candidate) -> str:
    """Return the text that may make claims, excluding repeated question context."""

    if evidence_claim_role(candidate) != "answer_with_question_context":
        return candidate.text
    match = _COMMENT_BOUNDARY.search(candidate.text)
    if match is None:
        return candidate.text
    return candidate.text[match.start(1) :]


def question_context_text(candidate: Candidate) -> str | None:
    """Return non-claim context that binds a comment answer to the original question."""

    if evidence_claim_role(candidate) != "answer_with_question_context":
        return None
    marker = "질문 문맥:"
    start = candidate.text.find(marker)
    boundary = _COMMENT_BOUNDARY.search(candidate.text)
    if start < 0 or boundary is None or start >= boundary.start(1):
        return None
    return candidate.text[start + len(marker) : boundary.start(1)].strip()


def claim_bearing_evidence(evidence: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    return tuple(
        candidate for candidate in evidence if evidence_claim_role(candidate) != "question_only"
    )


def article_source_key(candidate: Candidate) -> str | None:
    """Return the stable parent article key for a comment branch when available."""

    if evidence_claim_role(candidate) != "answer_with_question_context":
        return None
    stored = candidate.metadata.get("article_source_key")
    if isinstance(stored, str) and stored:
        return stored
    if candidate.source_key.startswith("comment:") and ":" in candidate.source_key[8:]:
        return candidate.source_key.removeprefix("comment:").rsplit(":", 1)[0]
    return None


def evidence_group_key(candidate: Candidate) -> str:
    """Group sibling comment branches without collapsing the parent article itself."""

    parent = article_source_key(candidate)
    if parent is not None:
        return f"comment-thread:{parent}"
    return f"source:{candidate.source_key}"


DEFAULT_RERANK_MAX_CHARS: int = 1500


def _slice_preserving_header(header: str, body: str, max_chars: int) -> str:
    """Slice body from the rear while preserving metadata header within max_chars."""
    if len(header) + len(body) <= max_chars:
        return f"{header}{body}"
    body_budget = max(0, max_chars - len(header))
    if body_budget > 0:
        return f"{header}{body[:body_budget]}"
    return header[:max_chars]


_METADATA_HEADER_PREFIXES: tuple[str, ...] = ("제목:", "게시판:", "분류:", "작성일:", "댓글 답변:")


def _extract_header_and_body(text: str) -> tuple[str, str]:
    """Extract metadata header and body from formatted article chunk text."""
    if not text.startswith("제목:"):
        return "", text
    if "\n\n" in text:
        header, body = text.split("\n\n", 1)
        return f"{header}\n\n", body
    lines = text.splitlines(keepends=True)
    header_lines: list[str] = []
    body_index = 0
    for i, line in enumerate(lines):
        if any(line.startswith(prefix) for prefix in _METADATA_HEADER_PREFIXES):
            header_lines.append(line)
            body_index = i + 1
        else:
            break
    if header_lines and body_index < len(lines):
        return "".join(header_lines), "".join(lines[body_index:])
    return "", text


def rerank_document(
    candidate: Candidate,
    *,
    max_chars: int = DEFAULT_RERANK_MAX_CHARS,
) -> str:
    """Rank answers by their claim text, sliced to fit reranker token window."""

    if evidence_claim_role(candidate) == "answer_with_question_context":
        title = str(candidate.metadata.get("title") or "제목 없음")
        board = str(candidate.metadata.get("board") or "게시판 미상")
        header = f"제목: {title}\n게시판: {board}\n댓글 답변:\n"
        body = claim_bearing_text(candidate)
        return _slice_preserving_header(header, body, max_chars)

    if len(candidate.text) <= max_chars:
        return candidate.text

    header, body = _extract_header_and_body(candidate.text)
    return _slice_preserving_header(header, body, max_chars)
