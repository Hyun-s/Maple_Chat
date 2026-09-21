from __future__ import annotations

import pytest

from maple_chat.agent.local_tools import build_local_tool_registry
from maple_chat.knowledge.service import KnowledgeFact
from maple_chat.retrieval.hybrid import Candidate, RetrievalResult


class _Retriever:
    async def retrieve(self, query: str) -> RetrievalResult:
        return RetrievalResult(
            (
                Candidate(
                    "community",
                    "article:1",
                    "커뮤니티 성공 사례 " + "가" * 900,
                    {
                        "title": "보스 공략",
                        "board": "팁",
                        "source_authority": "community",
                    },
                    fusion_score=0.5,
                ),
                Candidate(
                    "official",
                    "official:1",
                    "공식 업데이트 내용",
                    {
                        "title": "공식 패치",
                        "source_authority": "official",
                        "published_at": "2026-08-20",
                    },
                    fusion_score=0.8,
                    rerank_score=0.9,
                ),
            ),
            False,
        )


class _Knowledge:
    async def retrieve(self, query: str) -> tuple[KnowledgeFact, ...]:
        return (
            KnowledgeFact(
                relation_id="r1",
                subject_entity_id="job:xenon",
                subject_name="제논",
                subject_type="job",
                predicate="is_a",
                object_entity_id="family:hybrid",
                object_name="도적·해적 하이브리드",
                object_type="job_family",
                source_id="official",
                source_title="공식 지식",
                source_url="https://example.invalid",
                source_version="v1",
            ),
        )


@pytest.mark.asyncio
async def test_local_tools_separate_community_official_and_graph_results() -> None:
    registry = build_local_tool_registry(_Retriever(), _Knowledge())

    community = await registry.execute("search_community", {"query": "제논 보스"})
    official = await registry.execute("search_official_updates", {"query": "제논 변경"})
    graph = await registry.execute("query_knowledge_graph", {"query": "제논 직업군"})

    assert community["evidence"][0]["source_key"] == "article:1"  # type: ignore[index]
    assert len(community["evidence"][0]["excerpt"]) == 700  # type: ignore[index]
    assert official["evidence"][0]["source_key"] == "official:1"  # type: ignore[index]
    assert graph["relations"][0]["predicate"] == "is_a"  # type: ignore[index]
    assert set(registry.names) == {
        "search_community",
        "search_official_updates",
        "query_knowledge_graph",
    }
