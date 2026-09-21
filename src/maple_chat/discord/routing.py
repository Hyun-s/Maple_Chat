"""Transport-independent Discord access, mention, rate, and output rules."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from maple_chat.retrieval.modes import RAGQueryMode

_DANGEROUS_SCHEME = re.compile(r"(?i)\b(?:javascript|file|data):")
_RUN_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MessageEnvelope:
    guild_id: int | None
    channel_id: int
    author_id: int
    message_id: int
    content: str
    author_is_bot: bool = False
    from_webhook: bool = False


@dataclass(frozen=True, slots=True)
class RouteDecision:
    accepted: bool
    reason: str
    question: str | None = None
    rag_enabled: bool = True
    agent_enabled: bool = False
    agent_resume_id: str | None = None
    rag_mode: RAGQueryMode = RAGQueryMode.MIX


class DiscordAccessPolicy:
    def __init__(
        self,
        *,
        guild_ids: frozenset[int],
        channel_ids: frozenset[int],
        bot_user_id: int,
        blocked_user_ids: frozenset[int] = frozenset(),
        max_question_length: int = 1800,
    ) -> None:
        self.guild_ids = guild_ids
        self.channel_ids = channel_ids
        self.bot_user_id = bot_user_id
        self.blocked_user_ids = blocked_user_ids
        self.max_question_length = max_question_length

    def route(self, message: MessageEnvelope) -> RouteDecision:
        if message.author_is_bot or message.from_webhook:
            return RouteDecision(False, "non_user_message")
        if message.guild_id not in self.guild_ids:
            return RouteDecision(False, "guild_denied")
        if message.channel_id not in self.channel_ids:
            return RouteDecision(False, "channel_denied")
        if message.author_id in self.blocked_user_ids:
            return RouteDecision(False, "user_denied")
        mentions = (f"<@{self.bot_user_id}>", f"<@!{self.bot_user_id}>")
        if not any(mention in message.content for mention in mentions):
            return RouteDecision(False, "direct_mention_required")
        question = message.content
        for mention in mentions:
            question = question.replace(mention, " ")
        question = " ".join(question.split())
        rag_enabled = True
        rag_mode = RAGQueryMode.MIX
        agent_enabled = False
        agent_resume_id: str | None = None
        while True:
            option, separator, remainder = question.partition(" ")
            normalized_option = option.casefold()
            if normalized_option == "--no-rag":
                rag_enabled = False
                rag_mode = RAGQueryMode.BYPASS
            elif normalized_option == "--rag-mode":
                mode_value, mode_separator, mode_remainder = remainder.partition(" ")
                try:
                    rag_mode = RAGQueryMode(mode_value.casefold())
                except ValueError:
                    return RouteDecision(False, "invalid_rag_mode")
                rag_enabled = rag_mode is not RAGQueryMode.BYPASS
                question = mode_remainder.strip() if mode_separator else ""
                continue
            elif normalized_option == "--agent":
                agent_enabled = True
            elif normalized_option == "--agent-resume":
                run_id, run_separator, run_remainder = remainder.partition(" ")
                if not run_id or _RUN_ID.fullmatch(run_id) is None:
                    return RouteDecision(False, "invalid_agent_run_id")
                agent_enabled = True
                agent_resume_id = run_id.lower()
                question = (
                    run_remainder.strip() if run_separator else "이전 Agent 실행을 계속 진행해줘"
                )
                break
            else:
                break
            question = remainder.strip() if separator else ""
        if not question:
            return RouteDecision(False, "empty_question")
        if len(question) > self.max_question_length:
            return RouteDecision(False, "question_too_long")
        if _DANGEROUS_SCHEME.search(question):
            return RouteDecision(False, "dangerous_url_scheme")
        if question.count("<@") > 2:
            return RouteDecision(False, "too_many_mentions")
        return RouteDecision(
            accepted=True,
            reason="accepted",
            question=question,
            rag_enabled=rag_enabled,
            agent_enabled=agent_enabled,
            agent_resume_id=agent_resume_id,
            rag_mode=rag_mode,
        )


class SlidingWindowLimiter:
    def __init__(self, *, limit: int, window: timedelta) -> None:
        if limit < 1:
            raise ValueError("rate limit must be positive")
        self.limit = limit
        self.window = window
        self._events: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str, now: datetime) -> bool:
        queue = self._events[key]
        threshold = now - self.window
        while queue and queue[0] <= threshold:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(now)
        return True


def neutralize_mentions(value: str) -> str:
    return (
        value.replace("@everyone", "@\u200beveryone")
        .replace("@here", "@\u200bhere")
        .replace("<@", "<@\u200b")
    )


def split_discord_message(value: str, *, limit: int = 1900) -> tuple[str, ...]:
    if limit < 100 or limit > 2000:
        raise ValueError("Discord split limit is invalid")
    safe = neutralize_mentions(value).strip()
    if not safe:
        return ()
    parts: list[str] = []
    remaining = safe
    while len(remaining) > limit:
        boundary = max(
            remaining.rfind("\n\n", 0, limit + 1),
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if boundary < limit // 2:
            boundary = limit
        parts.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        parts.append(remaining)
    return tuple(parts)
