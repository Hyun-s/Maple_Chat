from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.admin import AdminDenied, AdminService
from maple_chat.db.models import (
    AnswerMode,
    AuditEvent,
    Chunk,
    CrawlJob,
    DenylistEntry,
    GuildSettings,
    SourceType,
)
from maple_chat.knowledge.service import (
    DatabaseKnowledgeRetriever,
    sync_builtin_job_catalog,
    sync_builtin_knowledge,
)
from maple_chat.llm.client import (
    ChatMessage,
    GenerationMetrics,
    GenerationResult,
    LLMUnavailable,
)
from maple_chat.qa.service import QAService, QuestionRequest, load_answer_sources
from maple_chat.retrieval.hybrid import Candidate, RetrievalResult

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


class FixedRetriever:
    def __init__(self, result: RetrievalResult) -> None:
        self.result = result
        self.calls = 0
        self.queries: list[str] = []

    async def retrieve(self, query: str) -> RetrievalResult:
        self.calls += 1
        self.queries.append(query)
        return self.result


class FixedGenerator:
    def __init__(
        self,
        *,
        unavailable: bool = False,
        responses: tuple[str, ...] = ("근거 기반 한국어 답변",),
    ) -> None:
        self.unavailable = unavailable
        self.responses = responses
        self.messages: tuple[ChatMessage, ...] | None = None
        self.message_history: list[tuple[ChatMessage, ...]] = []
        self.calls = 0

    async def generate(self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024) -> str:
        self.messages = messages
        self.message_history.append(messages)
        self.calls += 1
        if self.unavailable:
            raise LLMUnavailable("fixture outage")
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class MeasuredGenerator:
    async def generate(
        self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
    ) -> GenerationResult:
        return GenerationResult(
            text="측정된 답변",
            metrics=GenerationMetrics(
                prompt_tokens=120,
                completion_tokens=30,
                total_tokens=150,
                input_seconds=2.0,
                output_seconds=3.0,
                request_seconds=5.0,
            ),
        )


class MeasuredRepairGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
    ) -> GenerationResult:
        self.calls += 1
        texts = (
            "놀긍(놀라운 긍지)을 사용하세요.",
            "놀긍(놀라운 긍정의 혼돈 주문서)을 사용하세요.",
        )
        return GenerationResult(
            text=texts[min(self.calls - 1, 1)],
            metrics=GenerationMetrics(
                prompt_tokens=100 * self.calls,
                completion_tokens=10 * self.calls,
                total_tokens=110 * self.calls,
                input_seconds=float(self.calls),
                output_seconds=float(self.calls),
                request_seconds=2.0 * self.calls,
            ),
        )


async def seed_candidate(factory: async_sessionmaker[AsyncSession]) -> Candidate:
    candidate = Candidate(
        chunk_id="c" * 64,
        source_key="article:2304:1",
        text="정답 근거 본문",
        metadata={
            "title": "정확한 원 제목",
            "board": "팁과 노하우",
            "category": "사냥",
            "published_at": "2026-08-04T00:00:00Z",
            "url": "https://www.inven.co.kr/board/maple/2304/1",
        },
        fusion_score=0.2,
        rerank_score=0.9,
    )
    async with factory() as session, session.begin():
        session.add(
            Chunk(
                chunk_id=candidate.chunk_id,
                source_type=SourceType.ARTICLE,
                source_key=candidate.source_key,
                ordinal=0,
                text=candidate.text,
                token_count=3,
                metadata_json=candidate.metadata,
                chunking_revision="v1",
                active=True,
            )
        )
    return candidate


def request(message_id: int, now: datetime) -> QuestionRequest:
    return QuestionRequest(1, 10, message_id, "requester-hash", "사냥 팁?", now)


@pytest.mark.asyncio
async def test_direct_answer_skips_retrieval_and_has_no_sources(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    retriever = FixedRetriever(RetrievalResult((), True))
    generator = FixedGenerator()
    service = QAService(retriever, generator)
    direct_request = QuestionRequest(
        1,
        10,
        99,
        "requester-hash",
        "일반 질문",
        now,
        rag_enabled=False,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, direct_request)

    assert outcome.mode == AnswerMode.DIRECT
    assert outcome.sources == ()
    assert retriever.calls == 0
    assert generator.messages is not None
    assert "검색 증강 없이" in generator.messages[0].content


@pytest.mark.asyncio
async def test_measured_generation_reports_usage_throughput_and_total_time(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    service = QAService(FixedRetriever(RetrievalResult((), True)), MeasuredGenerator())
    direct_request = QuestionRequest(
        1,
        10,
        98,
        "requester-hash",
        "일반 질문",
        now,
        rag_enabled=False,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, direct_request)

    assert outcome.metrics is not None
    assert outcome.metrics.input_tokens == 120
    assert outcome.metrics.output_tokens == 30
    assert outcome.metrics.total_tokens == 150
    assert outcome.metrics.input_tokens_per_second == pytest.approx(60.0)
    assert outcome.metrics.output_tokens_per_second == pytest.approx(10.0)
    assert outcome.metrics.total_seconds > 0
    assert "입력 120 tok · 출력 30 tok · 총 150 tok" in outcome.text
    assert "입력 60.0 tok/s (TTFT 기반)" in outcome.text
    assert "출력 10.0 tok/s" in outcome.text
    assert "전체 " in outcome.text


@pytest.mark.asyncio
async def test_answer_transaction_preserves_exact_sources_and_duplicate_is_noop(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    candidate = await seed_candidate(factory)
    retriever = FixedRetriever(RetrievalResult((candidate,), False))
    service = QAService(retriever, FixedGenerator())
    async with factory() as session, session.begin():
        outcome = await service.answer(session, request(100, now))
    assert outcome.mode == AnswerMode.GENERATED
    assert outcome.text == "근거 기반 한국어 답변"
    assert retriever.queries == ["사냥 팁?"]

    async with factory() as session, session.begin():
        duplicate = await service.answer(session, request(100, now))
    assert duplicate.duplicate is True
    assert duplicate.answer_id == outcome.answer_id
    assert retriever.calls == 1

    async with factory() as session:
        sources = await load_answer_sources(
            session,
            answer_id=outcome.answer_id,
            requester_hash="requester-hash",
            now=now,
        )
        assert [source.title for source in sources] == ["정확한 원 제목"]
        assert sources[0].url == "https://www.inven.co.kr/board/maple/2304/1"
        with pytest.raises(PermissionError):
            await load_answer_sources(
                session,
                answer_id=outcome.answer_id,
                requester_hash="different-requester",
                now=now,
            )
        with pytest.raises(LookupError, match="expired"):
            await load_answer_sources(
                session,
                answer_id=outcome.answer_id,
                requester_hash="requester-hash",
                now=now + timedelta(days=8),
            )

    async with factory() as session, session.begin():
        chunk = await session.get(Chunk, candidate.chunk_id)
        assert chunk is not None
        chunk.active = False
    async with factory() as session:
        deleted = await load_answer_sources(
            session,
            answer_id=outcome.answer_id,
            requester_hash="requester-hash",
            now=now,
        )
        assert deleted[0].deleted is True
        assert deleted[0].url is None


@pytest.mark.asyncio
async def test_llm_failure_is_retrieval_only_and_low_evidence_refuses(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    candidate = await seed_candidate(factory)
    fallback = QAService(
        FixedRetriever(RetrievalResult((candidate,), False)), FixedGenerator(unavailable=True)
    )
    async with factory() as session, session.begin():
        fallback_outcome = await fallback.answer(session, request(101, now))
    assert fallback_outcome.mode == AnswerMode.RETRIEVAL_ONLY
    assert "AI 생성 답변이 아니며" in fallback_outcome.text

    refusal = QAService(FixedRetriever(RetrievalResult((), True)), FixedGenerator())
    async with factory() as session, session.begin():
        refusal_outcome = await refusal.answer(session, request(102, now))
    assert refusal_outcome.mode == AnswerMode.INSUFFICIENT_EVIDENCE
    assert refusal_outcome.sources == ()


@pytest.mark.asyncio
async def test_graph_only_evidence_generates_and_persists_official_provenance(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        await sync_builtin_job_catalog(session, synced_at=now)
    graph = DatabaseKnowledgeRetriever(factory)
    generator = FixedGenerator()
    service = QAService(
        FixedRetriever(RetrievalResult((), True)),
        generator,
        knowledge_retriever=graph,
    )
    graph_request = QuestionRequest(
        1,
        10,
        103,
        "requester-hash",
        "메카닉과 렌은 각각 무슨 직업군이야?",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, graph_request)

    assert outcome.mode == AnswerMode.GENERATED
    assert outcome.sources == ()
    assert {(fact.subject_name, fact.object_name) for fact in outcome.knowledge_facts} == {
        ("메카닉", "해적"),
        ("렌", "전사"),
    }
    assert outcome.has_sources is True
    assert generator.messages is not None
    assert "canonical_knowledge_json" in generator.messages[1].content

    async with factory() as session:
        sources = await load_answer_sources(
            session,
            answer_id=outcome.answer_id,
            requester_hash="requester-hash",
            now=now,
        )
    assert len(sources) == 1
    assert sources[0].board == "공식 지식"
    assert sources[0].url == "https://maplestory.nexon.com/Guide/N23Job"


@pytest.mark.asyncio
async def test_item_abbreviation_answer_preserves_curated_source_category(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)
    service = QAService(
        FixedRetriever(RetrievalResult((), True)),
        FixedGenerator(),
        knowledge_retriever=DatabaseKnowledgeRetriever(factory),
    )
    item_request = QuestionRequest(
        1,
        10,
        104,
        "requester-hash",
        "놀긍의 원래 아이템 이름은 뭐야?",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, item_request)

    assert [(fact.subject_name, fact.object_name) for fact in outcome.knowledge_facts] == [
        ("놀긍", "놀라운 긍정의 혼돈 주문서")
    ]
    async with factory() as session:
        sources = await load_answer_sources(
            session,
            answer_id=outcome.answer_id,
            requester_hash="requester-hash",
            now=now,
        )
    assert len(sources) == 1
    assert sources[0].category == "아이템 명칭"
    assert sources[0].title == "Maple Chat 검수 아이템 약어표"


@pytest.mark.asyncio
async def test_qa_comment_claim_enriches_graph_without_reusing_question_premise(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    candidate = Candidate(
        chunk_id="e" * 64,
        source_key="comment:2304:2:1",
        text=(
            "질문 문맥: 무조건 1. 추옵 2. 놀긍 3. 별 순서인가요?\n\n"
            "댓글 1: 순서가 중요하지는 않은데 놀긍으로 힘이 붙어야 별 강화 효과를 "
            "받습니다. 아크이노 후 다시 작해도 동일합니다."
        ),
        metadata={
            "title": "제논 추옵 작 스타포스 질문",
            "board": "질문과 답변",
            "qa_branch": True,
            "url": "https://www.inven.co.kr/board/maple/2304/2",
        },
        fusion_score=0.4,
        rerank_score=0.9,
    )
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)
        session.add(
            Chunk(
                chunk_id=candidate.chunk_id,
                source_type=SourceType.COMMENT,
                source_key=candidate.source_key,
                ordinal=0,
                text=candidate.text,
                token_count=40,
                metadata_json=candidate.metadata,
                chunking_revision="qa-role-test",
                active=True,
            )
        )
    generator = FixedGenerator()
    service = QAService(
        FixedRetriever(RetrievalResult((candidate,), False)),
        generator,
        knowledge_retriever=DatabaseKnowledgeRetriever(factory),
    )
    question = QuestionRequest(
        1,
        10,
        105,
        "requester-hash",
        "제논 방어구 직작 순서를 알려줘",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, question)

    relations = {
        (fact.subject_name, fact.predicate, fact.object_name) for fact in outcome.knowledge_facts
    }
    assert ("제논", "is_a", "도적") in relations
    assert ("제논", "is_a", "해적") in relations
    assert (
        "놀긍",
        "abbreviation_of",
        "놀라운 긍정의 혼돈 주문서",
    ) in relations
    assert ("아크이노", "abbreviation_of", "아크 이노센트 주문서") in relations
    assert generator.messages is not None
    assert "댓글 1: 순서가 중요하지는 않은데" in generator.messages[1].content
    assert (
        '"question_context":"무조건 1. 추옵 2. 놀긍 3. 별 순서인가요?"'
        in generator.messages[1].content
    )
    assert '"claim_text":"댓글 1: 순서가 중요하지는 않은데' in generator.messages[1].content
    assert "문맥일 뿐 사실 주장이 아니며 claim_text만" in generator.messages[0].content


@pytest.mark.asyncio
async def test_invalid_abbreviation_expansion_is_regenerated_from_canonical_relation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)
    generator = FixedGenerator(
        responses=(
            "놀긍(놀라운 긍지)을 사용하세요.",
            "놀긍(놀라운 긍정의 혼돈 주문서)을 사용하세요.",
        )
    )
    service = QAService(
        FixedRetriever(RetrievalResult((), True)),
        generator,
        knowledge_retriever=DatabaseKnowledgeRetriever(factory),
    )
    question = QuestionRequest(
        1,
        10,
        106,
        "requester-hash",
        "놀긍의 정식 이름은?",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, question)

    assert generator.calls == 2
    assert outcome.mode == AnswerMode.GENERATED
    assert outcome.text == "놀긍(놀라운 긍정의 혼돈 주문서)을 사용하세요."
    assert "correction_json" in generator.message_history[1][-1].content
    assert "놀라운 긍지" in generator.message_history[1][-1].content


@pytest.mark.asyncio
async def test_unregistered_parenthetical_guesses_are_removed_if_repair_is_ignored(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    candidate = await seed_candidate(factory)
    invalid = (
        "유에(유니온 챔피언)에서 레에(레전드)로 바꾸고 쌍레(쌍둥이 레전드)와 뚝(뚝딱이)을 맞추세요."
    )
    generator = FixedGenerator(responses=(invalid, invalid))
    service = QAService(
        FixedRetriever(RetrievalResult((candidate,), False)),
        generator,
    )
    question = QuestionRequest(
        1,
        10,
        108,
        "requester-hash",
        "제논 장비 세팅을 비교해 줘",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, question)

    assert generator.calls == 2
    assert outcome.mode == AnswerMode.GENERATED
    assert outcome.text == "유에에서 레에로 바꾸고 쌍레와 뚝을 맞추세요."
    assert "remove_parenthetical_explanation_and_keep_term" in (
        generator.message_history[1][-1].content
    )


@pytest.mark.asyncio
async def test_canonical_repair_aggregates_all_hidden_model_calls(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)
    generator = MeasuredRepairGenerator()
    service = QAService(
        FixedRetriever(RetrievalResult((), True)),
        generator,
        knowledge_retriever=DatabaseKnowledgeRetriever(factory),
    )
    question = QuestionRequest(
        1,
        10,
        107,
        "requester-hash",
        "놀긍의 정식 이름은?",
        now,
    )

    async with factory() as session, session.begin():
        outcome = await service.answer(session, question)

    assert generator.calls == 2
    assert outcome.metrics is not None
    assert outcome.metrics.model_calls == 2
    assert outcome.metrics.input_tokens == 300
    assert outcome.metrics.output_tokens == 30
    assert outcome.metrics.total_tokens == 330
    assert outcome.metrics.input_tokens_per_second == pytest.approx(100.0)
    assert outcome.metrics.output_tokens_per_second == pytest.approx(10.0)
    assert "입력 300 tok · 출력 30 tok · 총 330 tok" in outcome.text


@pytest.mark.asyncio
async def test_admin_is_owner_only_and_audited_without_raw_actor_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = AdminService(owner_id=1234, pii_salt="x" * 32)
    with pytest.raises(AdminDenied):
        admin.require_owner(9999)
    async with factory() as session, session.begin():
        await admin.audit(
            session,
            actor_id=1234,
            action="denylist.add",
            target="article:2304:1",
            result="succeeded",
        )
    async with factory() as session:
        event = await session.scalar(sa.select(AuditEvent))
        assert event is not None
        assert event.action == "denylist.add"
        assert event.actor_hash != "1234"


@pytest.mark.asyncio
async def test_operator_commands_require_impact_confirmation_and_report_status(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = AdminService(owner_id=1234, pii_salt="x" * 32)
    source_key = "article:2304:999"
    async with factory() as session, session.begin():
        with pytest.raises(ValueError, match="confirmation"):
            await admin.request_reindex(
                session,
                actor_id=1234,
                source_key=source_key,
                confirmation="wrong",
            )
        job_id = await admin.request_reindex(
            session,
            actor_id=1234,
            source_key=source_key,
            confirmation=source_key,
        )
        await admin.deny_article(
            session,
            actor_id=1234,
            board_id=2304,
            remote_article_id=999,
            reason="rights_request",
            confirmation=source_key,
        )
        await admin.configure_channels(
            session,
            actor_id=1234,
            guild_id=1,
            channel_ids=(10, 11),
        )
        status = await admin.status(session, actor_id=1234)
    assert status["queue_depth"] == 1
    async with factory() as session:
        assert await session.get(CrawlJob, job_id) is not None
        assert await session.scalar(sa.select(sa.func.count()).select_from(DenylistEntry)) == 1
        guild = await session.get(GuildSettings, 1)
        assert guild is not None
        assert guild.allowed_channel_ids == [10, 11]
