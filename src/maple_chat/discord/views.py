"""Persistent requester-only Discord source button."""

from __future__ import annotations

from typing import Protocol

import discord

from maple_chat.crawler.sanitizer import hash_nickname
from maple_chat.qa.service import SourceView


class SourceLoader(Protocol):
    async def load(self, answer_id: str, requester_hash: str) -> tuple[SourceView, ...]: ...


class _SourceButton(discord.ui.Button["SourceButtonView"]):
    def __init__(self, answer_id: str) -> None:
        super().__init__(
            label="출처 보기",
            style=discord.ButtonStyle.secondary,
            custom_id=f"maple:sources:{answer_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            return
        await self.view.show_sources(interaction)


def render_sources(sources: tuple[SourceView, ...]) -> str:
    if not sources:
        return "표시할 출처가 없습니다."
    lines: list[str] = []
    for source in sources:
        scope = " / ".join(value for value in (source.board, source.category) if value)
        suffix = "삭제됨" if source.deleted else source.url or "링크 없음"
        date = (source.published_at or "날짜 미상")[:10]
        lines.append(f"{source.rank}. {source.title} · {scope or '분류 없음'} · {date} · {suffix}")
    return "\n".join(lines)


class SourceButtonView(discord.ui.View):
    def __init__(self, *, answer_id: str, pii_salt: str, loader: SourceLoader) -> None:
        super().__init__(timeout=None)
        self.answer_id = answer_id
        self.pii_salt = pii_salt
        self.loader = loader
        self.add_item(_SourceButton(answer_id))

    async def show_sources(self, interaction: discord.Interaction) -> None:
        requester_hash = hash_nickname(str(interaction.user.id), self.pii_salt)
        try:
            sources = await self.loader.load(self.answer_id, requester_hash)
            message = render_sources(sources)
        except PermissionError:
            message = "이 출처는 원 질문자만 볼 수 있습니다."
        except LookupError:
            message = "출처 보기 유효기간이 만료되었습니다."
        safe_message = message.replace("@", "@\u200b")
        await interaction.response.send_message(
            safe_message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
