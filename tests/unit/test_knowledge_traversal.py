from __future__ import annotations

from datetime import UTC, datetime

import pytest

from maple_chat.db.models import KnowledgeRelation
from maple_chat.knowledge.service import _expanded_relation_ids


def _relation(relation_id: str, subject: str, object_: str) -> KnowledgeRelation:
    return KnowledgeRelation(
        relation_id=relation_id,
        subject_entity_id=subject,
        predicate="related_to",
        object_entity_id=object_,
        source_id="source",
        version="v1",
        metadata_json={},
        active=True,
        updated_at=datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_global_graph_expansion_is_bounded_by_hop_distance() -> None:
    relations = (
        _relation("r1", "a", "b"),
        _relation("r2", "b", "c"),
        _relation("r3", "c", "d"),
        _relation("unrelated", "x", "y"),
    )

    assert _expanded_relation_ids({"a"}, relations, max_hops=1) == {"r1"}
    assert _expanded_relation_ids({"a"}, relations, max_hops=2) == {"r1", "r2"}


def test_global_graph_expansion_rejects_unbounded_depth() -> None:
    with pytest.raises(ValueError, match="max_hops"):
        _expanded_relation_ids({"a"}, (), max_hops=3)
