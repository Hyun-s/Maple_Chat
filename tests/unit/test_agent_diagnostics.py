from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import pytest

from maple_chat.agent.service import DurableAgentService, NexonAgentService
from maple_chat.agent.store import StoredAgentRun
from maple_chat.agent.tools import ToolRegistry
from maple_chat.db.models import RunState
from maple_chat.llm.client import ChatMessage, GenerationMetrics, GenerationResult
from maple_chat.ops.logging import JsonFormatter
from maple_chat.qa.service import QuestionRequest
from maple_chat.time import utc_now


class SequenceGenerator:
    def __init__(self, responses: list[str | GenerationResult]) -> None:
        self.responses = responses

    async def generate(
        self, messages: tuple[ChatMessage, ...], *, max_tokens: int = 1024
    ) -> str | GenerationResult:
        return self.responses.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "category"),
    [
        ('{"action":"finish","answer":"private-canary}', "json_syntax"),
        ('{"private-canary":"secret-value"}', "schema_validation"),
        ("private-canary no object", "missing_json_object"),
    ],
)
async def test_decision_failure_logs_structural_details_without_raw_content(
    caplog: pytest.LogCaptureFixture, raw: str, category: str
) -> None:
    metrics = GenerationMetrics(20, 768, 788, 0.1, 1.0, 1.1)
    generated = GenerationResult(raw, metrics, finish_reason="length")
    engine = NexonAgentService(
        SequenceGenerator([generated, '{"action":"finish","answer":"done"}']),
        ToolRegistry(()),
    )

    with caplog.at_level(logging.WARNING, logger="maple_chat.agent.service"):
        result = await engine.run("private-question")

    records = [r for r in caplog.records if r.getMessage() == "agent_generation_parse_failed"]
    assert len(records) == 1
    rendered = JsonFormatter().format(records[0])
    payload = json.loads(rendered)
    assert payload["phase"] == "decision"
    assert payload["attempt"] == 1
    assert payload["error_category"] == category
    assert payload["response_chars"] == len(raw)
    assert payload["max_tokens"] == 768
    assert payload["finish_reason"] == "length"
    assert payload["completion_tokens"] == 768
    assert payload["completed_tool_calls"] == 0
    if category == "json_syntax":
        assert isinstance(payload["json_error_position"], int)
    if category == "schema_validation":
        assert "missing" in payload["validation_error_types"]
        assert "extra_forbidden" in payload["validation_error_types"]
    assert "private-canary" not in caplog.text
    assert "secret-value" not in rendered
    assert "private-canary" not in rendered
    assert "private-question" not in rendered
    assert "private-question" not in caplog.text
    assert "done" in result.text


@pytest.mark.asyncio
async def test_bounded_failures_report_attempts_and_completed_searches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = [
        '{"action":"tool","tool":"search_community","arguments":{"query":"one"}}',
        '{"action":"tool","tool":"search_community","arguments":{"query":"two"}}',
        "private-canary",
        '{"action":"finish"}',
    ]
    from pydantic import BaseModel

    from maple_chat.agent.tools import ToolSpec

    class Args(BaseModel):
        query: str

    async def search(_: BaseModel) -> dict:
        return {"evidence": "private-search-canary"}

    registry = ToolRegistry(
        (
            ToolSpec(
                name="search_community",
                description="search",
                input_model=Args,
                handler=search,
            ),
        )
    )
    engine = NexonAgentService(SequenceGenerator(responses), registry)
    with caplog.at_level(logging.WARNING, logger="maple_chat.agent.service"):
        result = await engine.run("카링 공략법좀 알ㄹ려주라")

    records = [r for r in caplog.records if r.getMessage() == "agent_generation_parse_failed"]
    rendered = "\n".join(JsonFormatter().format(r) for r in records)
    assert "private-canary" not in rendered
    assert "private-search-canary" not in rendered
    assert "private-question" not in rendered
    payloads = [json.loads(JsonFormatter().format(r)) for r in records]
    assert [p["attempt"] for p in payloads] == [1, 2]
    assert [p["completed_tool_calls"] for p in payloads] == [2, 2]
    assert payloads[1]["validation_error_types"] == ["value_error"]
    assert len(result.steps) == 2
    assert "응답 형식" in result.text
    assert "캐릭터명" not in result.text
    assert result.error_code == "invalid_decision"
    assert "private-search-canary" not in caplog.text
    assert "private-canary" not in caplog.text


@pytest.mark.asyncio
async def test_valid_generation_does_not_emit_failure_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = NexonAgentService(
        SequenceGenerator(['{"action":"finish","answer":"done"}']),
        ToolRegistry(()),
    )
    with caplog.at_level(logging.WARNING, logger="maple_chat.agent.service"):
        await engine.run("question")
    assert not [r for r in caplog.records if r.getMessage() == "agent_generation_parse_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["plan", "decision", "final"])
async def test_parse_failure_logs_phase_and_run_id(
    caplog: pytest.LogCaptureFixture, phase: str
) -> None:
    run_id = "2da43881-dfb7-4fa5-b0ac-3916665007ab"
    finish = '{"action":"finish","answer":"done"}'
    if phase == "plan":
        responses = ["private-canary", '{"goal":"goal","steps":["step"]}']
    elif phase == "final":
        responses = ['{"action":"tool","tool":"missing","arguments":{}}', "private-canary"]
    else:
        responses = ["private-canary", finish]
    engine = NexonAgentService(SequenceGenerator(responses), ToolRegistry(()), max_tool_calls=1)

    with caplog.at_level(logging.WARNING, logger="maple_chat.agent.service"):
        if phase == "plan":
            await engine.plan("private-question", run_id=run_id)
        else:
            await engine.run("private-question", run_id=run_id)

    records = [r for r in caplog.records if r.getMessage() == "agent_generation_parse_failed"]
    assert len(records) == 1
    rendered = JsonFormatter().format(records[0])
    payload = json.loads(rendered)
    assert payload["run_id"] == run_id
    assert payload["phase"] == phase
    assert payload["attempt"] == 1
    assert payload["max_tokens"] == {"plan": 512, "decision": 768, "final": 1024}[phase]
    assert payload["completed_tool_calls"] == (1 if phase == "final" else 0)
    assert payload["finish_reason"] is None
    assert payload["completion_tokens"] is None
    assert "private-canary" not in caplog.text


@pytest.mark.asyncio
async def test_durable_answer_correlates_plan_and_decision_failure_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stored = StoredAgentRun(
        run_id="2da43881-dfb7-4fa5-b0ac-3916665007ab",
        query="private-question",
        state=RunState.QUEUED,
        plan={},
        calls=(),
        max_tool_calls=4,
    )
    engine = NexonAgentService(
        SequenceGenerator(
            [
                "private-canary",
                '{"goal":"goal","steps":["step"]}',
                "private-canary",
                '{"action":"finish","answer":"done"}',
            ]
        ),
        ToolRegistry(()),
    )
    service = DurableAgentService(AsyncMock(), engine)
    store = AsyncMock()
    store.existing_answer.return_value = None
    store.create.return_value = stored
    store.save_plan.return_value = stored
    store.finish.return_value = "answer-id"
    service.store = store
    request = QuestionRequest(
        guild_id=1,
        channel_id=1,
        request_message_id=1,
        requester_hash="hash",
        query="private-question",
        received_at=utc_now(),
    )

    with caplog.at_level(logging.WARNING, logger="maple_chat.agent.service"):
        result = await service.answer(request)

    records = [r for r in caplog.records if r.getMessage() == "agent_generation_parse_failed"]
    rendered = "\n".join(JsonFormatter().format(r) for r in records)
    assert "private-canary" not in rendered
    assert "private-search-canary" not in rendered
    assert "private-question" not in rendered
    payloads = [json.loads(JsonFormatter().format(r)) for r in records]
    assert [p["phase"] for p in payloads] == ["plan", "decision"]
    assert all(p["run_id"] == stored.run_id for p in payloads)
    assert "done" in result.text
    assert "private-canary" not in caplog.text
    assert "private-question" not in caplog.text
