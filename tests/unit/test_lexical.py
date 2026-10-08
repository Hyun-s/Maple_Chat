from __future__ import annotations

import pytest

from maple_chat.retrieval import lexical


def test_extract_keywords_splits_korean_and_alphanumeric() -> None:
    keywords = lexical.extract_keywords("제논 하드 메이린 최소칸?! (공략)")
    assert keywords == ["제논", "하드", "메이린", "최소칸", "공략"]


def test_extract_keywords_handles_mixed_script_and_numbers() -> None:
    keywords = lexical.extract_keywords("BGE-M3 임베딩 6차 전직 260")
    assert keywords == ["BGE", "M3", "임베딩", "6차", "전직", "260"]


def test_extract_keywords_returns_empty_on_punctuation_only() -> None:
    assert lexical.extract_keywords("  !@#$%^&*()_+ ") == []


def test_build_tsquery_creates_prefix_or_expression() -> None:
    tsquery = lexical.build_tsquery("비숍 스킬트리")
    assert tsquery == "'비숍':* | '스킬트리':*"


def test_build_tsquery_returns_none_when_empty() -> None:
    assert lexical.build_tsquery("???") is None


@pytest.mark.asyncio
async def test_lexical_candidates_two_stage_success() -> None:
    executed: list[str] = []

    class FakeTransaction:
        async def __aenter__(self) -> FakeTransaction:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class FakeResult:
        def all(self) -> list[tuple[str, str, str, dict[str, object], float]]:
            return [("c1", "s1", "비숍 스킬트리 공략", {"title": "비숍"}, 0.75)]

    class FakeSession:
        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        async def execute(self, statement: object) -> FakeResult:
            executed.append(str(statement))
            return FakeResult()

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    candidates, query_seconds = await lexical.lexical_candidates(
        lambda: FakeSession(),  # type: ignore[arg-type]
        "비숍 스킬트리",
        50,
    )

    assert len(candidates) == 1
    assert candidates[0].chunk_id == "c1"
    assert candidates[0].lexical_score == 0.75
    assert candidates[0].text == "비숍 스킬트리 공략"
    assert query_seconds >= 0.0
    assert len(executed) == 1
    assert "to_tsvector" in executed[0]
    assert "similarity" in executed[0]


@pytest.mark.asyncio
async def test_lexical_candidates_fallback_on_empty_query() -> None:
    executed: list[str] = []

    class FakeTransaction:
        async def __aenter__(self) -> FakeTransaction:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class FakeResult:
        def all(self) -> list[tuple[object, float]]:
            return []

    class FakeSession:
        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        async def execute(self, statement: object) -> FakeResult:
            executed.append(str(statement))
            return FakeResult()

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    candidates, query_seconds = await lexical.lexical_candidates(
        lambda: FakeSession(),  # type: ignore[arg-type]
        "???",
        50,
    )

    assert candidates == []
    assert query_seconds >= 0.0
    # No tsquery can be built, so fallback runs directly with GUCs
    assert executed[:2] == list(lexical._LEXICAL_SCAN_GUC_STATEMENTS)
    assert "similarity" in executed[-1]
