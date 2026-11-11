from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from maple_chat.db.models import AnswerMode
from maple_chat.discord.routing import (
    DiscordAccessPolicy,
    MessageEnvelope,
    SlidingWindowLimiter,
    split_discord_message,
)
from maple_chat.discord.runtime import MapleDiscordClient
from maple_chat.qa.service import AnswerOutcome, SourceView
from maple_chat.retrieval.modes import RAGQueryMode


def envelope(**changes: object) -> MessageEnvelope:
    values: dict[str, object] = {
        "guild_id": 1,
        "channel_id": 10,
        "author_id": 20,
        "message_id": 30,
        "content": "<@99> 질문입니다",
    }
    values.update(changes)
    return MessageEnvelope(**values)  # type: ignore[arg-type]


@pytest.fixture
def policy() -> DiscordAccessPolicy:
    return DiscordAccessPolicy(
        guild_ids=frozenset({1, 2}), channel_ids=frozenset({10}), bot_user_id=99
    )


def test_only_direct_mentions_in_allowed_guild_and_channel_are_accepted(
    policy: DiscordAccessPolicy,
) -> None:
    decision = policy.route(envelope())
    assert decision.accepted is True
    assert decision.question == "질문입니다"
    assert policy.route(envelope(guild_id=2)).accepted is True
    assert policy.route(envelope(guild_id=3)).reason == "guild_denied"
    assert policy.route(envelope(channel_id=11)).reason == "channel_denied"
    assert policy.route(envelope(content="일반 대화")).reason == "direct_mention_required"
    assert policy.route(envelope(author_is_bot=True)).reason == "non_user_message"


def test_no_rag_option_is_removed_from_the_question_and_disables_retrieval(
    policy: DiscordAccessPolicy,
) -> None:
    decision = policy.route(envelope(content="<@99> --no-rag 메이플 말고 일반 질문"))

    assert decision.accepted is True
    assert decision.question == "메이플 말고 일반 질문"
    assert decision.rag_enabled is False
    assert decision.rag_mode is RAGQueryMode.BYPASS
    assert policy.route(envelope()).rag_enabled is True
    assert policy.route(envelope()).rag_mode is RAGQueryMode.MIX
    assert policy.route(envelope(content="<@99> --no-rag")).reason == "empty_question"


def test_rag_mode_option_selects_a_supported_rag_anything_query_mode(
    policy: DiscordAccessPolicy,
) -> None:
    decision = policy.route(envelope(content="<@99> --rag-mode global 직업 계층을 요약해줘"))

    assert decision.accepted is True
    assert decision.question == "직업 계층을 요약해줘"
    assert decision.rag_enabled is True
    assert decision.rag_mode is RAGQueryMode.GLOBAL

    invalid = policy.route(envelope(content="<@99> --rag-mode unknown 질문"))
    assert invalid.accepted is False
    assert invalid.reason == "invalid_rag_mode"


def test_rag_mode_and_agent_options_can_be_combined_in_either_order(
    policy: DiscordAccessPolicy,
) -> None:
    first = policy.route(envelope(content="<@99> --rag-mode naive --agent 질문"))
    second = policy.route(envelope(content="<@99> --agent --rag-mode local 질문"))

    assert first.question == "질문"
    assert first.agent_enabled is True
    assert first.rag_mode is RAGQueryMode.NAIVE
    assert second.question == "질문"
    assert second.agent_enabled is True
    assert second.rag_mode is RAGQueryMode.LOCAL


def test_agent_option_is_removed_and_selects_agent_route(policy: DiscordAccessPolicy) -> None:
    decision = policy.route(envelope(content="<@99> --agent 캐릭터 정보를 분석해줘"))

    assert decision.accepted is True
    assert decision.question == "캐릭터 정보를 분석해줘"
    assert decision.agent_enabled is True
    assert decision.rag_enabled is True


def test_agent_first_default_and_no_agent_or_qa_fallback(policy: DiscordAccessPolicy) -> None:
    default_decision = policy.route(envelope(content="<@99> 경험치 계산해줘"))
    assert default_decision.accepted is True
    assert default_decision.question == "경험치 계산해줘"
    assert default_decision.agent_enabled is True

    no_agent_decision = policy.route(envelope(content="<@99> --no-agent 단순 RAG 질문"))
    assert no_agent_decision.accepted is True
    assert no_agent_decision.question == "단순 RAG 질문"
    assert no_agent_decision.agent_enabled is False

    qa_decision = policy.route(envelope(content="<@99> --qa 단순 QA 질문"))
    assert qa_decision.accepted is True
    assert qa_decision.question == "단순 QA 질문"
    assert qa_decision.agent_enabled is False


def test_agent_resume_option_validates_and_extracts_run_id(
    policy: DiscordAccessPolicy,
) -> None:
    run_id = "12345678-1234-4234-8234-123456789abc"
    decision = policy.route(envelope(content=f"<@99> --agent-resume {run_id} 계속 분석해줘"))

    assert decision.accepted is True
    assert decision.agent_enabled is True
    assert decision.agent_resume_id == run_id
    assert decision.question == "계속 분석해줘"
    assert (
        policy.route(envelope(content="<@99> --agent-resume invalid")).reason
        == "invalid_agent_run_id"
    )


def test_empty_large_or_dangerous_questions_are_rejected(policy: DiscordAccessPolicy) -> None:
    assert policy.route(envelope(content="<@99> ")).reason == "empty_question"
    assert (
        policy.route(envelope(content="<@99> file:///tmp/value")).reason == "dangerous_url_scheme"
    )
    assert policy.route(envelope(content="<@99> " + "가" * 1801)).reason == "question_too_long"


def test_sliding_rate_limit_recovers_after_window() -> None:
    limiter = SlidingWindowLimiter(limit=2, window=timedelta(seconds=10))
    now = datetime(2026, 8, 4, tzinfo=UTC)
    assert limiter.allow("user:1", now) is True
    assert limiter.allow("user:1", now) is True
    assert limiter.allow("user:1", now) is False
    assert limiter.allow("user:1", now + timedelta(seconds=11)) is True


def test_long_output_is_split_and_all_mentions_are_neutralized() -> None:
    parts = split_discord_message(("문단 @everyone <@123>\n\n" + "가" * 300) * 8, limit=300)
    assert len(parts) > 1
    assert all(len(part) <= 300 for part in parts)
    assert all("@everyone" not in part and "<@123>" not in part for part in parts)


class _UnusedHandler:
    async def handle(
        self,
        message: MessageEnvelope,
        question: str,
        requester_hash: str,
        rag_enabled: bool,
        rag_mode: RAGQueryMode,
        agent_enabled: bool,
        agent_resume_id: str | None,
    ) -> AnswerOutcome:
        raise AssertionError("handler must not be called")


class _EmptySourceLoader:
    async def load(self, answer_id: str, requester_hash: str) -> tuple[SourceView, ...]:
        return ()

    async def active_answer_ids(self) -> tuple[str, ...]:
        return ()


@pytest.mark.asyncio
async def test_mention_only_client_does_not_request_privileged_message_content() -> None:
    client = MapleDiscordClient(
        guild_ids=frozenset({1, 2}),
        channel_ids=frozenset({10}),
        pii_salt="x" * 32,
        handler=_UnusedHandler(),
        source_loader=_EmptySourceLoader(),
    )
    try:
        assert client.intents.guilds is True
        assert client.intents.messages is True
        assert client.intents.message_content is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_database_answer_handler_passes_session_context_and_records_turn() -> None:
    from maple_chat.runtime import DatabaseAnswerHandler

    session_mgr = AsyncMock()
    session_mgr.build_context_prompt.return_value = "이전 대화 맥락"
    agent_service = AsyncMock()
    agent_service.answer.return_value = AnswerOutcome(
        answer_id="test-ans-1",
        mode=AnswerMode.GENERATED,
        text="계산 결과입니다.",
        sources=(),
    )
    handler = DatabaseAnswerHandler(
        factory=MagicMock(),
        service=AsyncMock(),
        agent_service=agent_service,
        session_manager=session_mgr,
    )
    env = envelope(guild_id=1, channel_id=10, author_id=20, message_id=30)
    outcome = await handler.handle(
        message=env,
        question="다음 레벨까지 몇 마리 잡아야 해?",
        requester_hash="hash123",
        rag_enabled=True,
        rag_mode=RAGQueryMode.HYBRID,
        agent_enabled=True,
        agent_resume_id=None,
    )

    assert outcome.text == "계산 결과입니다."
    session_mgr.build_context_prompt.assert_awaited_once_with("discord:10:20")
    agent_service.answer.assert_awaited_once()
    assert agent_service.answer.call_args.kwargs["context_prompt"] == "이전 대화 맥락"
    session_mgr.add_turn.assert_awaited_once_with(
        session_id="discord:10:20",
        user_query="다음 레벨까지 몇 마리 잡아야 해?",
        assistant_response="계산 결과입니다.",
    )
