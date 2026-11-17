"""Session contracts with fake Redis and a clock; no external services or writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import redis

from maple_chat.session import manager as sessions


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expiry: dict[str, int] = {}
        self.fail_get = False
        self.fail_set = False
        self.fail_delete = False
        self.fail_ping = False
        self.pings = 0

    def ping(self) -> bool:
        self.pings += 1
        if self.fail_ping:
            raise redis.ConnectionError("fixture unavailable")
        return True

    def get(self, key: str) -> bytes | None:
        if self.fail_get:
            raise redis.ConnectionError("fixture unavailable")
        return self.data.get(key)

    def set(self, key: str, value: str, *, ex: int) -> None:
        if self.fail_set:
            raise redis.ConnectionError("fixture unavailable")
        self.data[key] = value.encode()
        self.expiry[key] = ex

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise redis.ConnectionError("fixture unavailable")
        self.data.pop(key, None)


@pytest.mark.parametrize("options", [{"max_turns": 0}, {"history_limit": 0}])
def test_non_positive_history_limit_is_rejected(options: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="max_turns must be positive"):
        sessions.SessionManager(**options)


@pytest.mark.parametrize("unavailable", [False, True])
def test_host_connection_selects_redis_or_memory_without_real_network(
    unavailable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeRedis()
    client.fail_ping = unavailable
    calls: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> FakeRedis:
        calls.append(kwargs)
        return client

    monkeypatch.setattr(sessions.redis, "Redis", connect)
    manager = sessions.SessionManager(host="fixture.invalid", port=7777)
    assert calls == [{"host": "fixture.invalid", "port": 7777, "socket_connect_timeout": 1.0}]
    assert client.pings == 1
    assert manager.redis is (None if unavailable else client)


@pytest.mark.asyncio
async def test_redis_round_trip_limit_ttl_metadata_and_clear_are_session_scoped() -> None:
    client = FakeRedis()
    manager = sessions.SessionManager(
        redis_client=client, max_turns=5, history_limit=2, ttl_seconds=42
    )
    assert await manager.get_history("fixture-a") == ()
    for index in range(3):
        await manager.add_turn(
            "fixture-a", f"question-{index}", f"answer-{index}", {"index": index}
        )
    await manager.add_turn("fixture-b", "other question", "other answer")
    history = await manager.get_history("fixture-a")
    assert [turn.question for turn in history] == ["question-1", "question-2"]
    assert history[-1].metadata == {"index": 2}
    assert history[-1].created_at
    assert client.expiry["maple_chat:session:fixture-a"] == 42
    assert "question-2" in await manager.build_context_prompt("fixture-a")
    await manager.clear("fixture-a")
    assert await manager.get_history("fixture-a") == ()
    assert (await manager.get_history("fixture-b"))[0].question == "other question"


@pytest.mark.asyncio
async def test_get_and_set_outage_uses_memory_and_failed_delete_still_clears_it() -> None:
    client = FakeRedis()
    client.fail_get = client.fail_set = client.fail_delete = True
    manager = sessions.SessionManager(redis_client=client)
    await manager.append_turn("fixture-a", "question-a", "answer-a")
    await manager.append_turn("fixture-b", "question-b", "answer-b")
    assert (await manager.get_history("fixture-a"))[0].answer == "answer-a"
    await manager.clear("fixture-a")
    assert await manager.get_history("fixture-a") == ()
    assert (await manager.get_history("fixture-b"))[0].question == "question-b"
    assert client.data == {}


@pytest.mark.asyncio
async def test_invalid_cached_json_is_not_returned_as_history() -> None:
    client = FakeRedis()
    client.data["maple_chat:session:fixture-a"] = b"not-json"
    manager = sessions.SessionManager(redis_client=client)
    assert await manager.get_history("fixture-a") == ()


@pytest.mark.asyncio
async def test_memory_ttl_boundary_and_session_independence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(sessions, "time", SimpleNamespace(time=lambda: now[0]))
    manager = sessions.SessionManager(ttl_seconds=10)
    await manager.append_turn("fixture-a", "question-a", "answer-a")
    now[0] = 105.0
    await manager.append_turn("fixture-b", "question-b", "answer-b")
    now[0] = 110.0
    assert (await manager.get_history("fixture-a"))[0].question == "question-a"
    now[0] = 111.0
    assert await manager.get_history("fixture-a") == ()
    assert (await manager.get_history("fixture-b"))[0].question == "question-b"


def test_empty_turn_defaults_and_prompt_summary_bound() -> None:
    turn = sessions.ConversationTurn.from_dict({})
    assert turn.to_dict() == {"question": "", "answer": "", "created_at": "", "metadata": {}}
    manager = sessions.SessionManager()
    assert manager.format_history_for_prompt(()) == ""
    long_turn = sessions.ConversationTurn(question="fixture question", answer="X" * 305)
    prompt = manager.format_history_for_prompt((long_turn,))
    assert "fixture question" in prompt
    assert "X" * 300 + "..." in prompt
    assert "X" * 301 not in prompt
