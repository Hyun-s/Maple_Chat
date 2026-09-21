from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import KnowledgeEntity, KnowledgeRelation
from maple_chat.knowledge.service import (
    DatabaseKnowledgeRetriever,
    sync_builtin_job_catalog,
    sync_builtin_knowledge,
)

pytestmark = pytest.mark.skipif(
    "G002_DATABASE_URL" not in os.environ,
    reason="G002_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_catalog_sync_is_idempotent_and_graph_queries_are_exact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        first = await sync_builtin_job_catalog(session, synced_at=now)
    async with factory() as session, session.begin():
        second = await sync_builtin_job_catalog(session, synced_at=now)

    assert first == second
    assert first.entities == 54
    assert first.relations == 54
    async with factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(KnowledgeEntity)) == 54
        assert await session.scalar(sa.select(sa.func.count()).select_from(KnowledgeRelation)) == 54

    retriever = DatabaseKnowledgeRetriever(factory)
    xenon = await retriever.retrieve("제논은 무슨 직업군이야?")
    assert {(fact.subject_name, fact.object_name) for fact in xenon} == {
        ("제논", "도적"),
        ("제논", "해적"),
    }

    several = await retriever.retrieve("렌, 메카닉, 소마의 상위 직업을 알려줘")
    assert {(fact.subject_name, fact.object_name) for fact in several} == {
        ("렌", "전사"),
        ("메카닉", "해적"),
        ("소울마스터", "전사"),
    }

    mechanic_global = await retriever.retrieve("메카닉 직업 관계", max_hops=2)
    global_relations = {
        (fact.subject_name, fact.predicate, fact.object_name) for fact in mechanic_global
    }
    assert ("메카닉", "is_a", "해적") in global_relations
    assert ("해적", "part_of", "메이플스토리 직업 체계") in global_relations
    assert ("캡틴", "is_a", "해적") in global_relations

    pirates = await retriever.retrieve("해적 직업 목록을 알려줘")
    assert {fact.subject_name for fact in pirates if fact.predicate == "is_a"} >= {
        "메카닉",
        "제논",
        "아크",
    }

    all_jobs = await retriever.retrieve("전체 직업군별 하위 직업을 정리해줘")
    assert len([fact for fact in all_jobs if fact.predicate == "is_a"]) == 49
    assert len([fact for fact in all_jobs if fact.predicate == "part_of"]) == 5
    assert await retriever.retrieve("오늘 보스 공략 알려줘") == ()


@pytest.mark.asyncio
async def test_item_abbreviations_resolve_to_curated_official_names(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        result = await sync_builtin_knowledge(session, synced_at=now)

    assert result.sources == 7
    assert result.entities == 217
    assert result.aliases == 755
    assert result.relations == 292

    retriever = DatabaseKnowledgeRetriever(factory)
    facts = await retriever.retrieve("놀긍하고 프악공의 원래 아이템 이름이 뭐야?")
    assert {(fact.subject_name, fact.predicate, fact.object_name) for fact in facts} == {
        ("놀긍", "abbreviation_of", "놀라운 긍정의 혼돈 주문서"),
        ("프악공", "abbreviation_of", "프리미엄 악세서리 공격력 주문서"),
    }
    ark_innocence = await retriever.retrieve("아크이노는 무슨 주문서야?")
    assert {(fact.subject_name, fact.predicate, fact.object_name) for fact in ark_innocence} == {
        ("아크이노", "abbreviation_of", "아크 이노센트 주문서"),
        (
            "아크 이노센트 주문서",
            "has_effect",
            "잠재능력과 스타포스 강화를 제외한 모든 옵션을 표준 능력치로 초기화",
        ),
    }
    additional_option = await retriever.retrieve("추옵을 먼저 해야 해?")
    assert [(fact.subject_name, fact.object_name) for fact in additional_option] == [
        ("추옵", "추가 옵션")
    ]
    assert await retriever.retrieve("놀러긍이 뭐야?") == ()


@pytest.mark.asyncio
async def test_boss_variants_and_content_modes_do_not_cross_entity_boundaries(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    async with factory() as session, session.begin():
        await sync_builtin_knowledge(session, synced_at=now)

    retriever = DatabaseKnowledgeRetriever(factory)

    bellona = await retriever.retrieve("제논의 노말 벨로나 최소컷 배율은?")
    bellona_relations = {(fact.subject_name, fact.predicate, fact.object_name) for fact in bellona}
    assert ("제논", "is_a", "도적") in bellona_relations
    assert ("제논", "is_a", "해적") in bellona_relations
    assert ("벨로나 (노멀)", "variant_of", "벨로나") in bellona_relations
    assert all("메이린" not in fact.subject_name + fact.object_name for fact in bellona)

    all_bellona_modes = await retriever.retrieve("벨로나 난이도 알려줘")
    assert {fact.subject_name for fact in all_bellona_modes if fact.predicate == "variant_of"} == {
        "벨로나 (이지)",
        "벨로나 (노멀)",
        "벨로나 (하드)",
    }
    assert all("메이린" not in fact.subject_name + fact.object_name for fact in all_bellona_modes)

    meyrin = await retriever.retrieve("하드 메이린 최소컷")
    assert {(fact.subject_name, fact.predicate, fact.object_name) for fact in meyrin} == {
        ("메이린 (하드)", "variant_of", "메이린")
    }
    assert all("벨로나" not in fact.subject_name + fact.object_name for fact in meyrin)

    union = await retriever.retrieve("유챔이 뭐야?")
    assert {(fact.subject_name, fact.predicate, fact.object_name) for fact in union} >= {
        ("유챔", "abbreviation_of", "유니온 챔피언")
    }
    destiny = await retriever.retrieve("데스티니는 보스야?")
    assert {(fact.subject_type, fact.subject_name) for fact in destiny} >= {
        ("weapon", "데스티니 무기")
    }
    assert {
        fact.subject_name
        for fact in destiny
        if fact.predicate == "uses_mode" and fact.object_name == "데스티니 모드"
    } == {
        "선택받은 세렌 (데스티니 모드)",
        "감시자 칼로스 (데스티니 모드)",
        "카링 (데스티니 모드)",
    }

    destiny_seren = await retriever.retrieve("데스티니 세렌 조건 알려줘")
    assert {(fact.subject_name, fact.predicate, fact.object_name) for fact in destiny_seren} == {
        ("선택받은 세렌 (데스티니 모드)", "variant_of", "선택받은 세렌"),
        ("선택받은 세렌 (데스티니 모드)", "uses_mode", "데스티니 모드"),
        ("선택받은 세렌 (데스티니 모드)", "has_condition", "최종 데미지 80% 감소"),
        ("결전, 선택받은 세렌", "requires", "선택받은 세렌 (데스티니 모드)"),
    }
