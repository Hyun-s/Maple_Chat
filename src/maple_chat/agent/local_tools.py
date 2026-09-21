"""Read-only tools backed by Maple Chat retrieval and canonical knowledge."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from maple_chat.agent.tools import ToolRegistry, ToolSpec
from maple_chat.knowledge.service import KnowledgeFact
from maple_chat.retrieval.hybrid import Candidate, RetrievalResult


class EvidenceRetriever(Protocol):
    async def retrieve(self, query: str) -> RetrievalResult: ...


class KnowledgeRetriever(Protocol):
    async def retrieve(self, query: str) -> tuple[KnowledgeFact, ...]: ...


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=8)


def build_local_tool_registry(
    retriever: EvidenceRetriever,
    knowledge_retriever: KnowledgeRetriever,
) -> ToolRegistry:
    async def search_community(value: BaseModel) -> dict[str, object]:
        args = SearchInput.model_validate(value)
        retrieval = await retriever.retrieve(args.query)
        evidence = [
            _candidate_view(candidate)
            for candidate in retrieval.evidence
            if candidate.metadata.get("source_authority") != "official"
        ][: args.top_k]
        return {
            "query": args.query,
            "insufficient_evidence": not evidence,
            "evidence": evidence,
        }

    async def search_official_updates(value: BaseModel) -> dict[str, object]:
        args = SearchInput.model_validate(value)
        retrieval = await retriever.retrieve(args.query)
        evidence = [
            _candidate_view(candidate)
            for candidate in retrieval.evidence
            if candidate.metadata.get("source_authority") == "official"
        ][: args.top_k]
        return {
            "query": args.query,
            "insufficient_evidence": not evidence,
            "evidence": evidence,
        }

    async def query_knowledge_graph(value: BaseModel) -> dict[str, object]:
        args = SearchInput.model_validate(value)
        facts = await knowledge_retriever.retrieve(args.query)
        return {
            "query": args.query,
            "relations": [
                {
                    "relation_id": fact.relation_id,
                    "subject": fact.subject_name,
                    "subject_type": fact.subject_type,
                    "predicate": fact.predicate,
                    "object": fact.object_name,
                    "object_type": fact.object_type,
                    "source_title": fact.source_title,
                    "source_url": fact.source_url,
                    "source_version": fact.source_version,
                }
                for fact in facts[: args.top_k]
            ],
        }

    return ToolRegistry(
        (
            ToolSpec(
                name="search_community",
                description="저장·색인된 커뮤니티 공략과 경험 근거를 검색한다.",
                input_model=SearchInput,
                handler=search_community,
            ),
            ToolSpec(
                name="search_official_updates",
                description="저장·색인된 NEXON 공식 패치와 업데이트 근거를 검색한다.",
                input_model=SearchInput,
                handler=search_official_updates,
            ),
            ToolSpec(
                name="query_knowledge_graph",
                description="직업·보스·아이템·콘텐츠의 검수된 정형 관계를 조회한다.",
                input_model=SearchInput,
                handler=query_knowledge_graph,
            ),
        )
    )


def _candidate_view(candidate: Candidate) -> dict[str, object]:
    metadata = candidate.metadata
    text = candidate.text
    return {
        "source_key": candidate.source_key,
        "title": str(metadata.get("title") or "제목 없음"),
        "published_at": metadata.get("published_at"),
        "board": metadata.get("board"),
        "category": metadata.get("category"),
        "source_authority": metadata.get("source_authority", "community"),
        "excerpt": text[:700],
        "retrieval_score": candidate.fusion_score,
        "rerank_score": candidate.rerank_score,
    }
