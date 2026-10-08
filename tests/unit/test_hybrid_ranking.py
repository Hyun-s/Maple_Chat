from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maple_chat.retrieval import hybrid
from maple_chat.retrieval.evidence import (
    DEFAULT_RERANK_MAX_CHARS,
    article_source_key,
    evidence_group_key,
    rerank_document,
)
from maple_chat.retrieval.hybrid import (
    Candidate,
    calibrated_rerank_score,
    classify_query_hints,
    hybrid_retrieve,
    reciprocal_rank_fusion,
    title_query_coverage,
)


def candidate(chunk_id: str, source_key: str, **metadata: object) -> Candidate:
    return Candidate(chunk_id, source_key, chunk_id, dict(metadata))


def test_rrf_is_deterministic_and_quality_boost_cannot_dominate_relevance() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    dense = [candidate("a", "s1"), candidate("b", "s2")]
    lexical = [
        candidate(
            "b",
            "s2",
            recommendation_count=1_000_000,
            view_count=99_000_000,
            category="팁/정보",
            qa_branch=True,
            published_at="2026-08-04T00:00:00Z",
        ),
        candidate("a", "s1"),
    ]
    result = reciprocal_rank_fusion(dense, lexical, now=now)
    assert [item.chunk_id for item in result] == ["b", "a"]
    assert result[0].fusion_score - (1 / 61 + 1 / 61) <= 0.012


def test_official_source_authority_gets_a_bounded_additional_boost() -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    community = candidate("community", "article:2304:1")
    official = candidate(
        "official",
        "article:-1001:811",
        source_authority="official",
    )

    result = reciprocal_rank_fusion([community, official], [], now=now)

    assert [item.chunk_id for item in result] == ["official", "community"]
    assert result[0].fusion_score - 1 / 62 == pytest.approx(0.02)


def test_graph_linked_entity_in_title_outweighs_generic_quality_metadata() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    generic = candidate(
        "generic",
        "s1",
        title="하드 메이린 최소컷",
        recommendation_count=1_000_000,
        view_count=99_000_000,
        category="팁/정보",
        qa_branch=True,
        published_at="2026-08-25T00:00:00Z",
    )
    exact_job = candidate(
        "exact",
        "s2",
        title="제논 하드 메이린 배율 기록",
    )

    result = reciprocal_rank_fusion([generic, exact_job], [], now=now, anchor_terms=("제논",))

    assert [item.chunk_id for item in result] == ["exact", "generic"]


def test_graph_linked_entity_in_authored_article_body_gets_full_anchor_weight() -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    direct_report = Candidate(
        "direct",
        "article:2298:224856",
        "부캐 제논 하드 메이린을 93%로 클리어했습니다.",
        {"title": "하드 메이린 93% 클리어", "board": "해적"},
    )
    repeated_context = Candidate(
        "comment",
        "comment:article:2298:1:2",
        "질문 문맥: 제논 하드 메이린\n\n댓글 1: 축하합니다.",
        {"qa_branch": True, "title": "하드 메이린", "board": "해적"},
    )

    result = reciprocal_rank_fusion(
        [repeated_context, direct_report], [], now=now, anchor_terms=("제논",)
    )

    assert [item.chunk_id for item in result] == ["direct", "comment"]


def test_authored_article_title_can_recover_a_cross_encoder_false_negative() -> None:
    direct_report = Candidate(
        "direct",
        "article:2298:224946",
        "응애 하버 아버 없이 92퍼 정도 때 성공",
        {"title": "제논 뉴비 하드메이린92퍼 클리어", "board": "해적"},
    )
    sibling_comment = Candidate(
        "comment",
        "comment:article:2298:224946:1",
        "질문 문맥: 제논 하드메이린92퍼 클리어\n\n댓글 1: 축하합니다.",
        {
            "qa_branch": True,
            "title": "제논 뉴비 하드메이린92퍼 클리어",
            "board": "해적",
        },
    )

    assert title_query_coverage("제논의 하드메이린 최소컷", direct_report) > 0.3
    assert calibrated_rerank_score("제논의 하드메이린 최소컷", direct_report, 0.01) > 0.15
    assert calibrated_rerank_score(
        "제논의 하드메이린 최소컷", sibling_comment, 0.01
    ) == pytest.approx(0.01)


def test_query_hints_separate_test_and_live_server_language() -> None:
    assert classify_query_hints("테섭 최신 변경점") == {
        "test_server": True,
        "live_server": False,
        "as_of_requested": True,
    }
    assert classify_query_hints("본섭 기준 알려줘")["live_server"] is True


def test_comment_branches_share_a_thread_group_but_not_the_parent_article() -> None:
    first = Candidate(
        "c1",
        "comment:article:2298:225987:771487",
        "질문 문맥: 제논 하드 메이린 최소컷?\n\n댓글 1: 장비를 바꾸세요.",
        {"qa_branch": True, "title": "제논 질문", "board": "해적"},
    )
    second = Candidate(
        "c2",
        "comment:article:2298:225987:771489",
        "질문 문맥: 제논 하드 메이린 최소컷?\n\n댓글 1: 99% 클리어했습니다.",
        {
            "qa_branch": True,
            "article_source_key": "article:2298:225987",
            "title": "제논 질문",
            "board": "해적",
        },
    )
    article = Candidate(
        "a1",
        "article:2298:225987",
        "제논 하드 메이린 기록",
        {"title": "제논 질문", "board": "해적"},
    )

    assert article_source_key(first) == "article:2298:225987"
    assert evidence_group_key(first) == evidence_group_key(second)
    assert evidence_group_key(first) != evidence_group_key(article)


def test_comment_reranking_excludes_repeated_question_claims() -> None:
    candidate = Candidate(
        "c1",
        "comment:article:2298:225987:771487",
        "질문 문맥: 제논은 98%가 최소컷인가요?\n\n댓글 1: 장비 세팅만 답변합니다.",
        {"qa_branch": True, "title": "제논 질문", "board": "해적"},
    )

    document = rerank_document(candidate)

    assert "장비 세팅만 답변합니다" in document
    assert "98%가 최소컷" not in document


def test_rerank_document_preserves_article_header_and_slices_body_tail() -> None:
    header = "제목: 제논 보스 공략 가이드\n게시판: 도적\n분류: 팁\n작성일: 2026-10-08"
    long_body = "이것은 제논 보스 공략의 아주 긴 본문입니다. " * 100
    candidate = Candidate(
        "a1",
        "article:2298:100",
        f"{header}\n\n{long_body}",
        {"title": "제논 보스 공략 가이드", "board": "도적"},
    )

    assert len(candidate.text) > DEFAULT_RERANK_MAX_CHARS

    document = rerank_document(candidate)

    assert len(document) == DEFAULT_RERANK_MAX_CHARS
    assert document.startswith(f"{header}\n\n")
    assert "이것은 제논 보스 공략의 아주 긴 본문입니다." in document
    # Verify body tail was truncated
    assert not document.endswith(long_body[-50:])


def test_rerank_document_preserves_comment_header_and_slices_comment_tail() -> None:
    long_comment = "댓글 1: " + ("장비 세팅 상세 설명 및 보스 딜사이클 가이드 내용입니다. " * 80)
    candidate = Candidate(
        "c1",
        "comment:article:2298:225987:771487",
        f"질문 문맥: 무관한 질문 내용\n\n{long_comment}",
        {"qa_branch": True, "title": "제논 질문", "board": "해적"},
    )

    document = rerank_document(candidate)

    assert len(document) == DEFAULT_RERANK_MAX_CHARS
    assert document.startswith("제목: 제논 질문\n게시판: 해적\n댓글 답변:\n댓글 1: 장비 세팅")
    assert "무관한 질문 내용" not in document
    # Verify trailing part of comment was sliced
    assert not document.endswith(long_comment[-50:])


def test_rerank_document_respects_custom_max_chars() -> None:
    candidate = Candidate(
        "a1",
        "article:2298:100",
        "제목: 제목입니다\n게시판: 자유\n\n" + "내용 " * 200,
        {"title": "제목입니다", "board": "자유"},
    )

    document = rerank_document(candidate, max_chars=100)
    assert len(document) == 100
    assert document.startswith("제목: 제목입니다\n게시판: 자유\n\n")


def test_rerank_document_leaves_short_texts_untouched() -> None:
    short_text = "제목: 짧은 글\n게시판: 자유\n\n짧은 본문 내용입니다."
    candidate = Candidate("a1", "article:1:1", short_text, {})
    assert rerank_document(candidate) == short_text


@pytest.mark.asyncio
async def test_hybrid_retrieval_caps_sibling_comments_and_reranks_their_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sibling_comments = [
        Candidate(
            f"c{index}",
            f"comment:article:2298:1:{index}",
            f"질문 문맥: 제논 하드 메이린 98%가 최소컷인가요?\n\n댓글 1: 무관한 장비 답변 {index}",
            {"qa_branch": True, "title": "제논 질문", "board": "해적"},
            dense_score=1.0 - index / 100,
        )
        for index in range(5)
    ]
    sibling_comments[2] = Candidate(
        "c2",
        "comment:article:2298:1:2",
        "질문 문맥: 제논 하드 메이린 98%가 최소컷인가요?\n\n댓글 1: 99%로 클리어했습니다.",
        {"qa_branch": True, "title": "제논 질문", "board": "해적"},
        dense_score=0.98,
    )
    independent = Candidate(
        "a1",
        "article:2298:2",
        "제논 하드 메이린 93% 클리어 기록",
        {"title": "제논 하드 메이린 클리어", "board": "해적"},
        dense_score=0.5,
    )

    async def fake_dense(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [*sibling_comments, independent], 0.4

    async def fake_lexical(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [], 0.3

    class ClaimAwareReranker:
        documents: list[str]

        async def score(self, query: str, documents: list[str]) -> list[float]:
            self.documents = documents
            return [0.95 if "클리어" in document else 0.2 for document in documents]

    monkeypatch.setattr("maple_chat.retrieval.hybrid._dense_candidates", fake_dense)
    monkeypatch.setattr("maple_chat.retrieval.hybrid._lexical_candidates", fake_lexical)
    reranker = ClaimAwareReranker()

    result = await hybrid_retrieve(
        factory=object(),  # type: ignore[arg-type]
        query="제논 하드 메이린 최소컷",
        query_vector=[0.0] * 1024,
        reranker=reranker,
        now=datetime(2026, 8, 25, tzinfo=UTC),
        rerank_limit=4,
    )

    assert len([item for item in result.evidence if item.source_key.startswith("comment:")]) == 1
    assert {item.chunk_id for item in result.evidence} == {"c2", "a1"}
    assert all("98%가 최소컷" not in document for document in reranker.documents[:-1])


@pytest.mark.asyncio
async def test_named_boss_scope_excludes_other_boss_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_boss = Candidate(
        "meyrin",
        "article:2298:1",
        "제논이 하드 메이린을 98%로 클리어했습니다.",
        {"title": "제논 하드 메이린 기록", "board": "해적"},
        dense_score=0.99,
    )
    right_boss = Candidate(
        "bellona",
        "article:2298:2",
        "제논의 벨로나 기록을 찾고 있습니다.",
        {"title": "제논 벨로나 기록", "board": "해적"},
        dense_score=0.5,
    )
    wrong_job = Candidate(
        "phantom",
        "article:2297:3",
        "팬텀이 노멀 벨로나를 102.6%로 클리어했습니다.",
        {"title": "팬텀 노멀 벨로나 기록", "board": "도적"},
        dense_score=0.8,
    )

    async def fake_dense(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [wrong_boss, wrong_job, right_boss], 0.4

    async def fake_lexical(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [], 0.3

    class Reranker:
        async def score(self, query: str, documents: list[str]) -> list[float]:
            assert all("메이린" not in document for document in documents)
            return [0.8] * len(documents)

    monkeypatch.setattr("maple_chat.retrieval.hybrid._dense_candidates", fake_dense)
    monkeypatch.setattr("maple_chat.retrieval.hybrid._lexical_candidates", fake_lexical)

    result = await hybrid_retrieve(
        factory=object(),  # type: ignore[arg-type]
        query="제논의 벨로나 최소컷",
        query_vector=[0.0] * 1024,
        reranker=Reranker(),
        now=datetime(2026, 8, 25, tzinfo=UTC),
        required_scope_groups=(("제논",), ("벨로나",)),
    )

    assert [item.chunk_id for item in result.evidence] == ["bellona"]


@pytest.mark.asyncio
async def test_hybrid_retrieve_reports_rerank_stage_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_dense(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [candidate("c1", "article:1", title="제논 하드 메이린 공략")], 0.4

    async def fake_lexical(*args: object, **kwargs: object) -> tuple[list[Candidate], float]:
        return [], 0.3

    class ConstantReranker:
        async def score(self, query: str, documents: list[str]) -> list[float]:
            return [0.95] * len(documents)

    ticks = iter([10.0, 10.25, 100.0, 103.5])
    monkeypatch.setattr("maple_chat.retrieval.hybrid.perf_counter", lambda: next(ticks))
    monkeypatch.setattr("maple_chat.retrieval.hybrid._dense_candidates", fake_dense)
    monkeypatch.setattr("maple_chat.retrieval.hybrid._lexical_candidates", fake_lexical)

    result = await hybrid_retrieve(
        factory=object(),  # type: ignore[arg-type]
        query="제논 하드 메이린 최소컷",
        query_vector=[0.0] * 1024,
        reranker=ConstantReranker(),
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert result.rerank_seconds == pytest.approx(3.5)
    assert result.embedding_seconds == 0.0
    assert result.vector_query_seconds == pytest.approx(0.4)
    assert result.lexical_query_seconds == pytest.approx(0.3)
    assert result.merge_seconds == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_dense_candidates_pin_hnsw_ef_search_before_the_vector_scan() -> None:
    executed: list[str] = []
    events: list[str] = []

    class FakeTransaction:
        def __init__(self, name: str) -> None:
            self._name = name

        async def __aenter__(self) -> FakeTransaction:
            events.append(f"{self._name}:enter")
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            events.append(f"{self._name}:exit")
            return False

    class FakeResult:
        def all(self) -> list[tuple[object, float]]:
            return []

    class FakeSession:
        def begin(self) -> FakeTransaction:
            return FakeTransaction("transaction")

        async def execute(self, statement: object) -> FakeResult:
            events.append("execute")
            executed.append(str(statement))
            return FakeResult()

        async def __aenter__(self) -> FakeSession:
            events.append("session:enter")
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            events.append("session:exit")
            return False

    candidates, query_seconds = await hybrid._dense_candidates(
        lambda: FakeSession(),  # type: ignore[arg-type]
        [0.0] * 1024,
        1000,
    )

    assert candidates == []
    assert query_seconds >= 0.0
    assert events[:4] == ["session:enter", "transaction:enter", "execute", "execute"]
    assert executed[0] == f"SET LOCAL hnsw.ef_search = {hybrid._DENSE_EF_SEARCH}"
    assert "<=>" in executed[1]
    assert "ORDER BY" in executed[1]


@pytest.mark.asyncio
async def test_lexical_candidates_two_stage_prefiltering_with_similarity() -> None:
    executed: list[str] = []

    class FakeTransaction:
        async def __aenter__(self) -> FakeTransaction:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    class FakeResult:
        def all(self) -> list[tuple[str, str, str, dict[str, object], float]]:
            return [("c1", "s1", "제논 하드 메이린 컷", {}, 0.8)]

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

    candidates, query_seconds = await hybrid._lexical_candidates(
        lambda: FakeSession(),  # type: ignore[arg-type]
        "제논 하드 메이린 최소칸",
        200,
    )

    assert len(candidates) == 1
    assert candidates[0].chunk_id == "c1"
    assert candidates[0].lexical_score == 0.8
    assert query_seconds >= 0.0
    assert len(executed) == 1
    assert "to_tsvector" in executed[0]
    assert "similarity" in executed[0]
    assert "ORDER BY" in executed[0]


@pytest.mark.asyncio
async def test_lexical_candidates_widen_the_parallel_scan_before_the_trigram_sort() -> None:
    executed: list[str] = []
    events: list[str] = []

    class FakeTransaction:
        async def __aenter__(self) -> FakeTransaction:
            events.append("transaction:enter")
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            events.append("transaction:exit")
            return False

    class FakeResult:
        def all(self) -> list[tuple[object, float]]:
            return []

    class FakeSession:
        def begin(self) -> FakeTransaction:
            return FakeTransaction()

        async def execute(self, statement: object) -> FakeResult:
            events.append("execute")
            executed.append(str(statement))
            return FakeResult()

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    candidates, query_seconds = await hybrid._lexical_candidates(
        lambda: FakeSession(),  # type: ignore[arg-type]
        "제논 하드 메이린 최소칸",
        200,
    )

    assert candidates == []
    assert query_seconds >= 0.0
    # The two-stage prefilter executes first; on empty matches,
    # fallback executes GUCs and parallel scan
    assert "to_tsvector" in executed[0]
    assert executed[1:3] == list(hybrid._LEXICAL_SCAN_GUC_STATEMENTS)
    assert "similarity" in executed[-1]
    assert "ORDER BY" in executed[-1]
