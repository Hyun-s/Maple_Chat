from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from maple_chat.ops.health import ComponentHealth, collect_health, scheduler_freshness
from maple_chat.ops.logging import JsonFormatter, redact
from maple_chat.ops.metrics import MetricRegistry
from maple_chat.ops.shutdown import ShutdownCoordinator


def test_structured_logs_redact_secret_and_pii_canaries() -> None:
    record = logging.LogRecord(
        "maple_chat",
        logging.INFO,
        __file__,
        1,
        "contact user@example.invalid 010-1234-5678",
        (),
        None,
    )
    record.context = {
        "request_id": "safe-request-id",
        "discord_token": "never-log-this",  # pragma: allowlist secret
        "database_url": "postgresql://user:pass@db/value",  # pragma: allowlist secret
    }
    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)
    assert payload["request_id"] == "safe-request-id"
    assert "never-log-this" not in rendered
    assert "user:pass" not in rendered
    assert "example.invalid" not in rendered
    assert "010-1234-5678" not in rendered
    assert rendered.count("<redacted>") >= 2


def test_nested_redaction_never_preserves_sensitive_keys() -> None:
    assert redact({"payload": {"query": "raw question"}, "job_id": 3}) == {
        "payload": {"query": "<redacted>"},
        "job_id": 3,
    }


def test_structured_logs_preserve_numeric_model_telemetry_without_content() -> None:
    record = logging.LogRecord(
        "maple_chat.runtime",
        logging.INFO,
        __file__,
        1,
        "discord_answer_completed",
        (),
        None,
    )
    record.context = {
        "answer_id": "safe-answer-id",
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "input_tokens_per_second": 60.0,
        "output_tokens_per_second": 10.0,
        "total_seconds": 5.2,
    }

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "discord_answer_completed"
    assert payload["total_tokens"] == 150
    assert payload["input_tokens_per_second"] == 60.0
    assert payload["output_tokens_per_second"] == 10.0
    assert "query" not in payload
    assert "content" not in payload


@pytest.mark.asyncio
async def test_health_is_componentized_and_aggregate_fails_closed() -> None:
    async def healthy() -> ComponentHealth:
        return ComponentHealth("database", True, "ok", 1.0)

    async def unhealthy() -> ComponentHealth:
        return ComponentHealth("vllm", False, "unavailable")

    report = await collect_health((healthy, unhealthy))
    assert [item.name for item in report.components] == ["database", "vllm"]
    assert report.ready is False


def test_scheduler_freshness_and_metric_export() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    assert scheduler_freshness(last_success=now - timedelta(hours=1), now=now).healthy is True
    assert scheduler_freshness(last_success=None, now=now).detail == "never_succeeded"
    registry = MetricRegistry()
    registry.increment("discord_requests", result="success")
    registry.observe("retrieval_seconds", 0.25, stage="hybrid")
    output = registry.render()
    assert 'discord_requests_total{result="success"} 1' in output
    assert 'retrieval_seconds_count{stage="hybrid"} 1' in output
    assert 'retrieval_seconds_sum{stage="hybrid"} 0.25' in output


@pytest.mark.asyncio
async def test_shutdown_stops_new_work_and_is_bounded() -> None:
    coordinator = ShutdownCoordinator()
    blocker = asyncio.Event()

    async def work() -> object:
        await blocker.wait()
        return None

    task = asyncio.create_task(work())
    coordinator.track(task)
    assert await coordinator.shutdown(timeout_seconds=0.001) is False
    assert task.cancelled()
    rejected = asyncio.create_task(asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="shutdown"):
        coordinator.track(rejected)
    rejected.cancel()
    await asyncio.gather(rejected, return_exceptions=True)
