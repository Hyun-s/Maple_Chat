"""Sliding-window conversation session context manager backed by Redis with in-memory fallback."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import redis

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    question: str
    answer: str
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationTurn:
        return cls(
            question=data.get("question", ""),
            answer=data.get("answer", ""),
            created_at=data.get("created_at", ""),
            metadata=data.get("metadata", {}),
        )


class SessionManager:
    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        *,
        host: str | None = None,
        port: int = 6379,
        max_turns: int = 5,
        history_limit: int | None = None,
        ttl_seconds: int = 3600,
    ) -> None:
        turns = history_limit if history_limit is not None else max_turns
        if turns < 1:
            raise ValueError("max_turns must be positive")
        self.max_turns = turns
        self.ttl_seconds = ttl_seconds
        self._memory_store: dict[str, tuple[float, list[dict[str, Any]]]] = {}

        self.redis: redis.Redis | None
        if redis_client is not None:
            self.redis = redis_client
        elif host is not None:
            try:
                r = redis.Redis(host=host, port=port, socket_connect_timeout=1.0)
                r.ping()
                self.redis = r
            except Exception as err:
                logger.warning(
                    "Redis connection to %s:%s failed (%s), using in-memory store",
                    host,
                    port,
                    err,
                )
                self.redis = None
        else:
            self.redis = None

    def _redis_key(self, session_key: str) -> str:
        return f"maple_chat:session:{session_key}"

    async def get_history(self, session_key: str) -> tuple[ConversationTurn, ...]:
        key = self._redis_key(session_key)
        if self.redis is not None:
            try:
                raw = self.redis.get(key)
                if raw is not None:
                    items = json.loads(raw)
                    return tuple(ConversationTurn.from_dict(item) for item in items)
                return ()
            except Exception as err:
                logger.warning("Redis session get failed, using memory store: %s", err)

        # In-memory fallback
        now = time.time()
        entry = self._memory_store.get(session_key)
        if entry is None:
            return ()
        updated_at, items = entry
        if now - updated_at > self.ttl_seconds:
            self._memory_store.pop(session_key, None)
            return ()
        return tuple(ConversationTurn.from_dict(item) for item in items)

    async def append_turn(
        self,
        session_key: str,
        question: str,
        answer: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        turn = ConversationTurn(
            question=question,
            answer=answer,
            metadata=metadata or {},
        )
        history = list(await self.get_history(session_key))
        history.append(turn)
        trimmed = history[-self.max_turns :]
        data = [t.to_dict() for t in trimmed]
        serialized = json.dumps(data, ensure_ascii=False)

        key = self._redis_key(session_key)
        if self.redis is not None:
            try:
                self.redis.set(key, serialized, ex=self.ttl_seconds)
                return
            except Exception as err:
                logger.warning("Redis session set failed, using memory store: %s", err)

        # In-memory fallback
        self._memory_store[session_key] = (time.time(), data)

    async def clear(self, session_key: str) -> None:
        key = self._redis_key(session_key)
        if self.redis is not None:
            try:
                self.redis.delete(key)
            except Exception as err:
                logger.warning("Redis session delete failed: %s", err)
        self._memory_store.pop(session_key, None)

    def format_history_for_prompt(self, turns: tuple[ConversationTurn, ...]) -> str:
        if not turns:
            return ""
        lines = ["[최근 대화 맥락]"]
        for index, turn in enumerate(turns, 1):
            lines.append(f"- 사용자 ({index}): {turn.question}")
            # 너무 긴 답변은 앞 250자로 축약하여 프롬프트 토큰 절약
            summary = turn.answer.strip()
            if len(summary) > 300:
                summary = summary[:300] + "..."
            lines.append(f"  답변: {summary}")
        lines.append(
            "이전 대화에서 언급된 레벨, 닉네임, 사냥터 등의 맥락이 현재 "
            "질문과 이어지는 경우 이를 반영하여 구체적으로 답한다."
        )
        return "\n".join(lines)

    async def build_context_prompt(self, session_key: str) -> str:
        history = await self.get_history(session_key)
        return self.format_history_for_prompt(history)

    async def add_turn(
        self,
        session_id: str,
        user_query: str,
        assistant_response: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.append_turn(
            session_key=session_id,
            question=user_query,
            answer=assistant_response,
            metadata=metadata,
        )
