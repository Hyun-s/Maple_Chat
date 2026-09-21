"""discord.py transport adapter for the tested routing and QA domains."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Protocol

import discord

from maple_chat.crawler.sanitizer import hash_nickname
from maple_chat.discord.routing import (
    DiscordAccessPolicy,
    MessageEnvelope,
    SlidingWindowLimiter,
    split_discord_message,
)
from maple_chat.discord.views import SourceButtonView, SourceLoader
from maple_chat.qa.service import AnswerOutcome
from maple_chat.retrieval.modes import RAGQueryMode
from maple_chat.time import utc_now


class RuntimeAnswerHandler(Protocol):
    async def handle(
        self,
        message: MessageEnvelope,
        question: str,
        requester_hash: str,
        rag_enabled: bool,
        rag_mode: RAGQueryMode,
        agent_enabled: bool,
        agent_resume_id: str | None,
    ) -> AnswerOutcome: ...


class PersistentSourceLoader(SourceLoader, Protocol):
    async def active_answer_ids(self) -> tuple[str, ...]: ...


class MapleDiscordClient(discord.Client):
    def __init__(
        self,
        *,
        guild_ids: frozenset[int],
        channel_ids: frozenset[int],
        pii_salt: str,
        handler: RuntimeAnswerHandler,
        source_loader: PersistentSourceLoader,
        max_waiting: int = 20,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        # Discord includes content for messages that directly mention the app, which is the
        # only accepted input route. Do not request the privileged broad message-content intent.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_ids = guild_ids
        self.channel_ids = channel_ids
        self.pii_salt = pii_salt
        self.handler = handler
        self.source_loader = source_loader
        self.max_waiting = max_waiting
        self._generation = asyncio.Semaphore(1)
        self._queue_lock = asyncio.Lock()
        self._waiting = 0
        self._user_limiter = SlidingWindowLimiter(limit=5, window=timedelta(minutes=1))
        self._channel_limiter = SlidingWindowLimiter(limit=20, window=timedelta(minutes=1))

    async def setup_hook(self) -> None:
        for answer_id in await self.source_loader.active_answer_ids():
            self.add_view(
                SourceButtonView(
                    answer_id=answer_id,
                    pii_salt=self.pii_salt,
                    loader=self.source_loader,
                )
            )

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None:
            return
        envelope = MessageEnvelope(
            guild_id=message.guild.id if message.guild else None,
            channel_id=message.channel.id,
            author_id=message.author.id,
            message_id=message.id,
            content=message.content,
            author_is_bot=message.author.bot,
            from_webhook=message.webhook_id is not None,
        )
        policy = DiscordAccessPolicy(
            guild_ids=self.guild_ids,
            channel_ids=self.channel_ids,
            bot_user_id=self.user.id,
        )
        decision = policy.route(envelope)
        if not decision.accepted or decision.question is None:
            return
        now = utc_now()
        if not self._user_limiter.allow(
            f"user:{message.author.id}", now
        ) or not self._channel_limiter.allow(f"channel:{message.channel.id}", now):
            await message.reply(
                "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", mention_author=False
            )
            return
        async with self._queue_lock:
            if self._waiting >= self.max_waiting:
                await message.reply("현재 답변 대기열이 가득 찼습니다.", mention_author=False)
                return
            self._waiting += 1
        try:
            async with message.channel.typing(), self._generation:
                requester_hash = hash_nickname(str(message.author.id), self.pii_salt)
                outcome = await self.handler.handle(
                    envelope,
                    decision.question,
                    requester_hash,
                    decision.rag_enabled,
                    decision.rag_mode,
                    decision.agent_enabled,
                    decision.agent_resume_id,
                )
            if outcome.duplicate:
                return
            parts = split_discord_message(outcome.text)
            for index, part in enumerate(parts):
                view = None
                if index == len(parts) - 1 and outcome.has_sources:
                    view = SourceButtonView(
                        answer_id=outcome.answer_id,
                        pii_salt=self.pii_salt,
                        loader=self.source_loader,
                    )
                if view is None:
                    await message.reply(
                        part,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await message.reply(
                        part,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        view=view,
                    )
        finally:
            async with self._queue_lock:
                self._waiting -= 1
