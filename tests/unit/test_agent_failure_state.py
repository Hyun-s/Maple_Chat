from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from maple_chat.agent.service import DurableAgentService, NexonAgentService
from maple_chat.agent.store import AgentRunStore, StoredAgentRun, StoredToolCall
from maple_chat.agent.tools import ToolRegistry
from maple_chat.db.models import RunState
from maple_chat.llm.client import ChatMessage, GenerationMetrics, GenerationResult
from maple_chat.qa.service import QuestionRequest
from maple_chat.time import utc_now


class SequenceGenerator:
    def __init__(self, responses: list[str | GenerationResult]) -> None:
        self.responses = responses
        self.calls = 0

    async def generate(
        self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
    ) -> str | GenerationResult:
        self.calls += 1
        return self.responses.pop(0)


def request() -> QuestionRequest:
    return QuestionRequest(
        guild_id=1,
        channel_id=1,
        request_message_id=1,
        requester_hash="hash",
        query="카링 공략법좀 알ㄹ려주라",
        received_at=utc_now(),
    )


def durable_service(
    responses: list[str | GenerationResult],
    *,
    max_tool_calls: int = 4,
) -> tuple[DurableAgentService, AsyncMock, SequenceGenerator]:
    generator = SequenceGenerator(responses)
    engine = NexonAgentService(generator, ToolRegistry(()), max_tool_calls=max_tool_calls)
    service = DurableAgentService(AsyncMock(), engine)
    calls = (
        StoredToolCall(1, "search_community", {"query": "카링 패턴"}, {"evidence": "one"}, False),
        StoredToolCall(2, "search_community", {"query": "카링 기믹"}, {"evidence": "two"}, False),
    )
    stored = StoredAgentRun(
        run_id="2da43881-dfb7-4fa5-b0ac-3916665007ab",
        query=request().query,
        state=RunState.PARTIAL,
        plan={"goal": "카링 공략", "steps": ["검색", "요약"]},
        calls=calls,
        max_tool_calls=max_tool_calls,
    )
    store = AsyncMock(spec=AgentRunStore)
    store.existing_answer.return_value = None
    store.create.return_value = stored
    store.resume.return_value = stored
    store.finish.return_value = "answer-id"
    service.store = store
    return service, store, generator


@pytest.mark.asyncio
async def test_invalid_decision_marks_durable_run_failed_and_preserves_metrics() -> None:
    metrics = GenerationMetrics(20, 768, 788, 0.1, 1.0, 1.1)
    malformed = GenerationResult("not-json", metrics, finish_reason="length")
    service, store, generator = durable_service([malformed, malformed])

    outcome = await service.answer(request())

    assert generator.calls == 2
    assert "응답 형식" in outcome.text
    assert "캐릭터명" not in outcome.text
    assert "--agent-resume 2da43881-dfb7-4fa5-b0ac-3916665007ab" in outcome.text
    store.fail.assert_awaited_once()
    assert store.fail.await_args.kwargs["error_code"] == "invalid_decision"
    assert store.finish.await_args.kwargs["succeeded"] is False
    assert [call[0] for call in store.mock_calls] == [
        "existing_answer",
        "create",
        "fail",
        "finish",
    ]
    assert outcome.metrics is not None
    assert outcome.metrics.model_calls == 2
    assert outcome.metrics.output_tokens == 1536


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["not-json", '{"action":"tool","tool":"unused"}'])
async def test_invalid_forced_final_marks_durable_run_failed(raw: str) -> None:
    service, store, generator = durable_service([raw], max_tool_calls=2)

    outcome = await service.answer(request())

    assert generator.calls == 1
    assert "캐릭터명" not in outcome.text
    assert "--agent-resume" in outcome.text
    store.fail.assert_awaited_once()
    assert store.fail.await_args.kwargs["error_code"] == "invalid_final_answer"
    assert store.finish.await_args.kwargs["succeeded"] is False
    store.record_tool_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tool_calls", [2, 4])
async def test_valid_final_remains_successful_without_failure_or_resume_hint(
    max_tool_calls: int,
) -> None:
    service, store, generator = durable_service(
        ['{"action":"finish","answer":"카링 공략 답변"}'],
        max_tool_calls=max_tool_calls,
    )

    outcome = await service.answer(request())

    assert generator.calls == 1
    assert "카링 공략 답변" in outcome.text
    assert "--agent-resume" not in outcome.text
    store.fail.assert_not_awaited()
    store.finish.assert_awaited_once()
    assert store.finish.await_args.kwargs["succeeded"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["decision", "final"])
async def test_real_store_transitions_failure_duplicate_and_resume_without_database(
    failure_kind: str,
) -> None:
    """Exercise real store methods over an in-memory session boundary, not a live DB."""
    from dataclasses import replace
    from datetime import timedelta
    from unittest.mock import MagicMock

    from maple_chat.db.models import AgentCallState, AgentRun, AgentToolCall, Answer

    req = request()
    run = AgentRun(
        run_id="2da43881-dfb7-4fa5-b0ac-3916665007ab",
        guild_id=1,
        channel_id=1,
        request_message_id=req.request_message_id,
        requester_hash=req.requester_hash,
        query=req.query,
        query_hash="hash",
        state=RunState.PARTIAL,
        plan_json={"goal": "카링 공략", "steps": ["검색", "요약"]},
        checkpoint_json={"next_action": "evaluate", "last_tool": "search_community"},
        tool_call_count=2,
        max_tool_calls=4,
        resume_count=0,
        created_at=req.received_at,
        updated_at=req.received_at,
        expires_at=req.received_at + timedelta(days=7),
    )
    tool_calls = [
        AgentToolCall(
            run_id=run.run_id,
            step_index=i,
            tool_name="search_community",
            arguments_json={"query": str(i)},
            result_json={"evidence": str(i)},
            state=AgentCallState.SUCCEEDED,
            created_at=req.received_at,
        )
        for i in (1, 2)
    ]
    answers: dict[int, Answer] = {}

    async def scalar(statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is AgentRun:
            return run
        if entity is Answer:
            message_id = statement.compile().params["request_message_id_1"]
            return answers.get(message_id)
        raise AssertionError("unexpected query")

    def add(answer):
        assert isinstance(answer, Answer)
        answers[answer.request_message_id] = answer

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock()
    transaction.__aexit__ = AsyncMock(return_value=False)
    session.begin.return_value = transaction
    session.execute = AsyncMock()
    session.scalar = AsyncMock(side_effect=scalar)
    session.scalars = AsyncMock(return_value=tool_calls)
    session.get = AsyncMock(return_value=run)
    session.flush = AsyncMock()
    session.add.side_effect = add
    factory = MagicMock(return_value=session)
    invalid_responses = (
        ["not-json", '{"action":"finish"}'] if failure_kind == "decision" else ["not-json"]
    )
    failed_call_count = len(invalid_responses)
    generator = SequenceGenerator(
        [*invalid_responses, '{"action":"finish","answer":"카링 공략 답변"}']
    )
    limit = 4 if failure_kind == "decision" else 2
    run.max_tool_calls = limit
    service = DurableAgentService(
        factory, NexonAgentService(generator, ToolRegistry(()), max_tool_calls=limit)
    )

    failed = await service.answer(req)
    assert run.state is RunState.FAILED
    assert run.error_code == (
        "invalid_decision" if failure_kind == "decision" else "invalid_final_answer"
    )
    assert run.checkpoint_json["next_action"] == "resume"
    assert run.checkpoint_json["last_tool"] == "search_community"
    assert run.tool_call_count == 2
    assert "캐릭터명" not in failed.text

    duplicate = await service.answer(req)
    assert duplicate.duplicate is True
    assert generator.calls == failed_call_count

    resumed = await service.answer(replace(req, request_message_id=2), resume_run_id=run.run_id)
    assert run.state is RunState.SUCCEEDED
    assert run.error_code is None
    assert run.resume_count == 1
    assert run.tool_call_count == 2
    assert generator.calls == failed_call_count + 1
    assert "카링 공략 답변" in resumed.text
    assert run.checkpoint_json["next_action"] == "complete"
