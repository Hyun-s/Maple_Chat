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


def rerank_document(candidate: Candidate) -> str:
    """Rank answers by their claim text, not by duplicated question text."""

    if evidence_claim_role(candidate) != "answer_with_question_context":
        return candidate.text
    title = str(candidate.metadata.get("title") or "제목 없음")
    board = str(candidate.metadata.get("board") or "게시판 미상")
    return f"제목: {title}\n게시판: {board}\n댓글 답변:\n{claim_bearing_text(candidate)}"
