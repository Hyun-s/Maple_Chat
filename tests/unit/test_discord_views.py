from __future__ import annotations

from typing import Any

import pytest

from maple_chat.crawler.sanitizer import hash_nickname
from maple_chat.discord.views import SourceButtonView, render_sources
from maple_chat.qa.service import SourceView


class Loader:
    def __init__(self, result: tuple[SourceView, ...] | Exception) -> None:
        self.result = result
        self.request: tuple[str, str] | None = None

    async def load(self, answer_id: str, requester_hash: str) -> tuple[SourceView, ...]:
        self.request = (answer_id, requester_hash)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Response:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def send_message(self, message: str, **kwargs: object) -> None:
        self.kwargs = {"message": message, **kwargs}


class Interaction:
    def __init__(self, user_id: int) -> None:
        self.user = type("User", (), {"id": user_id})()
        self.response = Response()


def source(*, deleted: bool = False) -> SourceView:
    return SourceView(
        rank=1,
        title="정확한 출처",
        board="팁과 노하우",
        category="사냥",
        published_at="2026-08-05T00:00:00Z",
        url=None if deleted else "https://www.inven.co.kr/board/maple/2304/1",
        deleted=deleted,
    )


def test_render_sources_handles_empty_active_and_deleted_rows() -> None:
    assert render_sources(()) == "표시할 출처가 없습니다."
    assert "정확한 출처" in render_sources((source(),))
    assert "삭제됨" in render_sources((source(deleted=True),))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ((source(),), "정확한 출처"),
        (PermissionError(), "원 질문자만"),
        (LookupError(), "유효기간이 만료"),
    ],
)
async def test_source_button_is_requester_scoped_and_ephemeral(
    result: tuple[SourceView, ...] | Exception,
    expected: str,
) -> None:
    loader = Loader(result)
    interaction = Interaction(123)
    view = SourceButtonView(answer_id="answer-1", pii_salt="x" * 32, loader=loader)

    await view.show_sources(interaction)  # type: ignore[arg-type]

    assert expected in interaction.response.kwargs["message"]
    assert interaction.response.kwargs["ephemeral"] is True
    if not isinstance(result, Exception):
        assert loader.request == ("answer-1", hash_nickname("123", "x" * 32))


@pytest.mark.asyncio
async def test_button_callback_delegates_to_its_view() -> None:
    loader = Loader((source(),))
    interaction = Interaction(123)
    view = SourceButtonView(answer_id="answer-1", pii_salt="x" * 32, loader=loader)
    button = view.children[0]

    await button.callback(interaction)  # type: ignore[arg-type]

    assert "정확한 출처" in interaction.response.kwargs["message"]
