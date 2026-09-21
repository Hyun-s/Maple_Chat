from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from maple_chat.knowledge.service import KnowledgeFact
from maple_chat.llm.client import ChatMessage
from maple_chat.qa.service import QAService, QuestionRequest
from maple_chat.retrieval.hybrid import Candidate, RetrievalResult
from maple_chat.retrieval.modes import RAGQueryMode


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, _statement: object) -> None:
        return None

    async def scalar(self, _statement: object) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _EvidenceRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.candidate = Candidate(
            chunk_id="c1",
            source_key="article:1",
            text="메카닉 관련 커뮤니티 근거",
            metadata={"title": "메카닉 공략"},
            fusion_score=0.2,
            rerank_score=0.9,
        )

    async def retrieve(self, query: str, *, use_knowledge_anchors: bool = True) -> RetrievalResult:
        self.calls.append((query, use_knowledge_anchors))
        return RetrievalResult((self.candidate,), False)


class _KnowledgeRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fact = KnowledgeFact(
            relation_id="r1",
            subject_entity_id="job:mechanic",
            subject_name="메카닉",
            subject_type="job",
            predicate="is_a",
            object_entity_id="job-family:pirate",
            object_name="해적",
            object_type="job_family",
            source_id="source",
            source_title="공식 직업 소개",
            source_url="https://maplestory.nexon.com/Guide/N23Job",
            source_version="v1",
        )

    async def retrieve(self, query: str, *, max_hops: int = 1) -> tuple[KnowledgeFact, ...]:
        self.calls.append((query, max_hops))
        return (self.fact,)


class _Generator:
    def __init__(self) -> None:
        self.calls: list[tuple[ChatMessage, ...]] = []

    async def generate(self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024) -> str:
        self.calls.append(messages)
        return "근거 기반 답변"


def _request(mode: RAGQueryMode) -> QuestionRequest:
    return QuestionRequest(
        guild_id=1,
        channel_id=2,
        request_message_id=list(RAGQueryMode).index(mode) + 1,
        requester_hash="requester",
        query="메카닉은 어떤 직업이야?",
        received_at=datetime(2026, 9, 5, tzinfo=UTC),
        rag_mode=mode,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "evidence_calls", "knowledge_hops"),
    [
        (RAGQueryMode.LOCAL, 0, [1]),
        (RAGQueryMode.GLOBAL, 0, [2]),
        (RAGQueryMode.HYBRID, 1, [2]),
        (RAGQueryMode.NAIVE, 1, []),
        (RAGQueryMode.MIX, 1, [1, 1]),
    ],
)
async def test_qa_service_executes_the_selected_query_plan(
    mode: RAGQueryMode,
    evidence_calls: int,
    knowledge_hops: list[int],
) -> None:
    evidence = _EvidenceRetriever()
    knowledge = _KnowledgeRetriever()
    generator = _Generator()
    service = QAService(evidence, generator, knowledge_retriever=knowledge)

    outcome = await service.answer(_Session(), _request(mode))  # type: ignore[arg-type]

    assert len(evidence.calls) == evidence_calls
    assert [max_hops for _, max_hops in knowledge.calls] == knowledge_hops
    evidence_modes = {RAGQueryMode.HYBRID, RAGQueryMode.NAIVE, RAGQueryMode.MIX}
    assert bool(outcome.sources) is (mode in evidence_modes)
    assert bool(outcome.knowledge_facts) is (mode is not RAGQueryMode.NAIVE)
    if mode is RAGQueryMode.NAIVE:
        assert evidence.calls == [("메카닉은 어떤 직업이야?", False)]


@pytest.mark.asyncio
async def test_bypass_mode_skips_all_retrieval() -> None:
    evidence = _EvidenceRetriever()
    knowledge = _KnowledgeRetriever()
    generator = _Generator()
    service = QAService(evidence, generator, knowledge_retriever=knowledge)

    outcome = await service.answer(
        _Session(),
        _request(RAGQueryMode.BYPASS),  # type: ignore[arg-type]
    )

    assert evidence.calls == []
    assert knowledge.calls == []
    assert outcome.sources == ()
    assert outcome.knowledge_facts == ()
    assert "검색 증강 없이" in generator.calls[0][0].content


class _TimedEvidenceRetriever(_EvidenceRetriever):
    async def retrieve(self, query: str, *, use_knowledge_anchors: bool = True) -> RetrievalResult:
        result = await super().retrieve(query, use_knowledge_anchors=use_knowledge_anchors)
        return replace(result, embedding_seconds=0.5, rerank_seconds=2.25)


@pytest.mark.asyncio
async def test_retrieval_stage_seconds_reach_processing_metrics() -> None:
    service = QAService(_TimedEvidenceRetriever(), _Generator())

    outcome = await service.answer(_Session(), _request(RAGQueryMode.MIX))  # type: ignore[arg-type]

    metrics = outcome.metrics
    assert metrics is not None
    assert metrics.embedding_seconds == pytest.approx(0.5)
    assert metrics.rerank_seconds == pytest.approx(2.25)
    assert metrics.retrieval_seconds is not None
    assert metrics.retrieval_seconds > 0.0
    assert metrics.total_seconds >= metrics.retrieval_seconds


@pytest.mark.asyncio
async def test_bypass_mode_reports_no_retrieval_stage_seconds() -> None:
    service = QAService(_EvidenceRetriever(), _Generator())

    outcome = await service.answer(
        _Session(),
        _request(RAGQueryMode.BYPASS),  # type: ignore[arg-type]
    )

    metrics = outcome.metrics
    assert metrics is not None
    assert metrics.embedding_seconds is None
    assert metrics.rerank_seconds is None
    assert metrics.retrieval_seconds is None
