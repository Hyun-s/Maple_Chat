"""RAG-Anything-compatible query modes mapped onto Maple Chat retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RAGQueryMode(StrEnum):
    """Public retrieval modes supported by RAG-Anything and LightRAG."""

    LOCAL = "local"
    GLOBAL = "global"
    HYBRID = "hybrid"
    NAIVE = "naive"
    MIX = "mix"
    BYPASS = "bypass"


@dataclass(frozen=True, slots=True)
class RAGQueryPlan:
    uses_evidence: bool
    uses_knowledge: bool
    graph_hops: int
    links_evidence_entities: bool
    uses_knowledge_anchors: bool


_PLANS = {
    RAGQueryMode.LOCAL: RAGQueryPlan(False, True, 1, False, False),
    RAGQueryMode.GLOBAL: RAGQueryPlan(False, True, 2, False, False),
    RAGQueryMode.HYBRID: RAGQueryPlan(True, True, 2, False, True),
    RAGQueryMode.NAIVE: RAGQueryPlan(True, False, 0, False, False),
    RAGQueryMode.MIX: RAGQueryPlan(True, True, 1, True, True),
    RAGQueryMode.BYPASS: RAGQueryPlan(False, False, 0, False, False),
}


def query_plan(mode: RAGQueryMode) -> RAGQueryPlan:
    """Return the bounded Maple Chat execution plan for a public query mode."""

    return _PLANS[mode]
