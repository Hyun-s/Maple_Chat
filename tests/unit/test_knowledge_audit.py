from __future__ import annotations

from maple_chat.knowledge.audit import compound_surfaces, expansion_candidates


def test_expansion_candidates_only_extract_explicit_parenthetical_or_assignment_forms() -> None:
    text = "놀긍(놀러긍)은 잘못된 설명이고 놀긍 = 놀라운 긍정의 혼돈 주문서라고 다시 설명했다."

    assert expansion_candidates(text, "놀긍") == (
        "놀러긍",
        "놀라운 긍정의 혼돈 주문서라고 다시 설명했다",
    )
    assert expansion_candidates("놀긍떡작을 했다", "놀긍") == ()


def test_compound_surfaces_decode_historical_nested_html_entities() -> None:
    text = "놀긍떡작&amp;nbsp;이후 프악공작"

    assert compound_surfaces(text, "놀긍") == ("놀긍떡작",)
    assert compound_surfaces(text, "프악공") == ("프악공작",)
