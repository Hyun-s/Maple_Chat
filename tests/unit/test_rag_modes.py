from __future__ import annotations

from maple_chat.retrieval.modes import RAGQueryMode, query_plan


def test_rag_query_modes_follow_the_rag_anything_retrieval_contract() -> None:
    assert query_plan(RAGQueryMode.LOCAL).uses_knowledge is True
    assert query_plan(RAGQueryMode.LOCAL).uses_evidence is False
    assert query_plan(RAGQueryMode.LOCAL).graph_hops == 1

    assert query_plan(RAGQueryMode.GLOBAL).uses_knowledge is True
    assert query_plan(RAGQueryMode.GLOBAL).uses_evidence is False
    assert query_plan(RAGQueryMode.GLOBAL).graph_hops == 2

    assert query_plan(RAGQueryMode.HYBRID).uses_knowledge is True
    assert query_plan(RAGQueryMode.HYBRID).uses_evidence is True
    assert query_plan(RAGQueryMode.HYBRID).graph_hops == 2
    assert query_plan(RAGQueryMode.HYBRID).links_evidence_entities is False

    assert query_plan(RAGQueryMode.NAIVE).uses_knowledge is False
    assert query_plan(RAGQueryMode.NAIVE).uses_evidence is True
    assert query_plan(RAGQueryMode.NAIVE).uses_knowledge_anchors is False

    assert query_plan(RAGQueryMode.MIX).uses_knowledge is True
    assert query_plan(RAGQueryMode.MIX).uses_evidence is True
    assert query_plan(RAGQueryMode.MIX).graph_hops == 1
    assert query_plan(RAGQueryMode.MIX).links_evidence_entities is True

    assert query_plan(RAGQueryMode.BYPASS).uses_knowledge is False
    assert query_plan(RAGQueryMode.BYPASS).uses_evidence is False
