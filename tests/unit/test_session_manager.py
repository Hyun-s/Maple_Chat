from __future__ import annotations

import pytest
import redis
from maple_chat.session.manager import ConversationTurn, SessionManager


@pytest.mark.asyncio
async def test_in_memory_session_sliding_window() -> None:
    manager = SessionManager(redis_client=None, max_turns=3, ttl_seconds=10)
    session_id = "test-channel:user-123"

    assert await manager.get_history(session_id) == ()

    # 1번째 턴 추가
    await manager.append_turn(session_id, "나 260렙인데 사냥 어디서 해?", "세르니움 전초기지나 불타는 세르니움을 추천합니다.")
    history = await manager.get_history(session_id)
    assert len(history) == 1
    assert history[0].question == "나 260렙인데 사냥 어디서 해?"

    # 2번째, 3번째 턴 추가
    await manager.append_turn(session_id, "거기 마릿수는 얼마나 나와?", "시간당 약 15,000마리 내외로 측정됩니다.")
    await manager.append_turn(session_id, "극성비 쓰면 몇 퍼센트 올라?", "약 8.98% 상승합니다.")
    history = await manager.get_history(session_id)
    assert len(history) == 3

    # 4번째 턴 추가 -> max_turns가 3이므로 1번째 턴은 밀려나야 함 (Sliding Window FIFO)
    await manager.append_turn(session_id, "2재획 하면 업할 수 있어?", "2재획 시 약 5.7% 상승하므로 추가 사냥이 필요합니다.")
    history = await manager.get_history(session_id)
    assert len(history) == 3
    assert history[0].question == "거기 마릿수는 얼마나 나와?"
    assert history[1].question == "극성비 쓰면 몇 퍼센트 올라?"
    assert history[2].question == "2재획 하면 업할 수 있어?"

    # 프롬프트 포맷 확인
    prompt_text = manager.format_history_for_prompt(history)
    assert "[최근 대화 맥락]" in prompt_text
    assert "거기 마릿수는 얼마나 나와?" in prompt_text
    assert "2재획 하면 업할 수 있어?" in prompt_text

    # 세션 초기화
    await manager.clear(session_id)
    assert await manager.get_history(session_id) == ()


@pytest.mark.asyncio
async def test_redis_session_manager() -> None:
    try:
        r = redis.Redis(host="127.0.0.1", port=6379, socket_connect_timeout=1.0)
        r.ping()
    except Exception:
        pytest.skip("Redis not available")

    manager = SessionManager(redis_client=r, max_turns=3, ttl_seconds=60)
    session_id = "integration-test:user-456"
    await manager.clear(session_id)

    await manager.append_turn(session_id, "질문 1", "답변 1")
    await manager.append_turn(session_id, "질문 2", "답변 2")
    history = await manager.get_history(session_id)
    assert len(history) == 2
    assert history[0].question == "질문 1"
    assert history[1].question == "질문 2"

    await manager.clear(session_id)
    assert await manager.get_history(session_id) == ()
