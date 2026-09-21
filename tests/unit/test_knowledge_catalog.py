from __future__ import annotations

from maple_chat.knowledge.catalog import (
    load_boss_catalog,
    load_destiny_weapon_catalog,
    load_game_term_catalog,
    load_item_abbreviation_catalog,
    load_job_catalog,
    load_meyrin_boss_catalog,
    load_union_champion_catalog,
    normalize_alias,
)
from maple_chat.knowledge.service import AliasRecord, select_entity_matches


def test_builtin_job_catalog_is_complete_and_provenance_bearing() -> None:
    catalog = load_job_catalog()
    entities = {entity.entity_id: entity for entity in catalog.entities}
    relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in catalog.relations
    }

    assert catalog.source.publisher == "NEXON Korea"
    assert catalog.source.url == "https://maplestory.nexon.com/Guide/N23Job"
    assert len([entity for entity in catalog.entities if entity.entity_type == "job"]) == 48
    assert len([entity for entity in catalog.entities if entity.entity_type == "job_family"]) == 5
    assert entities["job:mechanic"].canonical_name == "메카닉"
    assert ("job:mechanic", "is_a", "job-family:pirate") in relations
    assert ("job:len", "is_a", "job-family:warrior") in relations
    assert ("job:soul-master", "is_a", "job-family:warrior") in relations
    assert ("job:xenon", "is_a", "job-family:thief") in relations
    assert ("job:xenon", "is_a", "job-family:pirate") in relations
    assert len(catalog.content_hash) == 64


def test_entity_linking_is_catalog_driven_longest_match_and_ontology_specific() -> None:
    aliases = (
        AliasRecord("아크", "job:ark", "job"),
        AliasRecord("아크메이지(불,독)", "job:fire-poison", "job"),
        AliasRecord("불독", "job:fire-poison", "job"),
        AliasRecord("소마", "job:soul-master", "job"),
        AliasRecord("전사", "job-family:warrior", "job_family"),
        AliasRecord("직업군", "taxonomy:jobs", "taxonomy"),
    )

    longest = select_entity_matches("아크메이지(불,독) 장비", aliases)
    assert [match.entity_id for match in longest] == ["job:fire-poison"]

    specific = select_entity_matches("소마는 전사 직업군이야?", aliases)
    assert [match.entity_id for match in specific] == ["job:soul-master"]
    assert normalize_alias("  데몬   슬레이어 ") == "데몬 슬레이어"


def test_entity_linking_keeps_named_job_and_boss_in_independent_ontology_facets() -> None:
    aliases = (
        AliasRecord("제논", "job:xenon", "job"),
        AliasRecord("직업군", "taxonomy:jobs", "taxonomy"),
        AliasRecord("벨로나", "boss:bellona", "boss"),
        AliasRecord("노말 벨로나", "boss-variant:bellona-normal", "boss_variant"),
    )

    matches = select_entity_matches("제논 직업군의 노말 벨로나 최소컷", aliases)

    assert [match.entity_id for match in matches] == [
        "job:xenon",
        "boss-variant:bellona-normal",
    ]


def test_generic_boss_taxonomy_does_not_expand_over_a_concrete_non_boss_entity() -> None:
    aliases = (
        AliasRecord("데스티니 무기", "weapon:destiny", "weapon"),
        AliasRecord("보스", "taxonomy:bosses", "taxonomy"),
    )

    matches = select_entity_matches("데스티니 무기는 보스야?", aliases)

    assert [match.entity_id for match in matches] == ["weapon:destiny"]


def test_intentional_homonym_keeps_both_meanings_until_a_longer_alias_disambiguates() -> None:
    aliases = (
        AliasRecord("데스티니", "weapon:destiny", "weapon"),
        AliasRecord("데스티니", "content-mode:destiny", "content_mode"),
        AliasRecord("데스티니 난이도", "content-mode:destiny", "content_mode"),
    )

    ambiguous = select_entity_matches("데스티니가 뭐야?", aliases)
    difficulty = select_entity_matches("데스티니 난이도 알려줘", aliases)

    assert {match.entity_id for match in ambiguous} == {
        "weapon:destiny",
        "content-mode:destiny",
    }
    assert [match.entity_id for match in difficulty] == ["content-mode:destiny"]


def test_item_abbreviation_catalog_separates_curated_alias_from_official_name() -> None:
    catalog = load_item_abbreviation_catalog()
    entities = {entity.entity_id: entity for entity in catalog.entities}
    relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in catalog.relations
    }

    assert catalog.source.publisher == "Maple Chat maintainers"
    assert (
        entities["item:amazing-positive-chaos-scroll"].canonical_name == "놀라운 긍정의 혼돈 주문서"
    )
    assert entities["abbreviation:amazing-positive-chaos-scroll"].canonical_name == "놀긍"
    assert (
        "abbreviation:amazing-positive-chaos-scroll",
        "abbreviation_of",
        "item:amazing-positive-chaos-scroll",
    ) in relations
    assert "놀러긍" not in {entity.canonical_name for entity in catalog.entities}


def test_game_term_catalog_maps_community_term_to_official_term() -> None:
    catalog = load_game_term_catalog()
    entities = {entity.entity_id: entity for entity in catalog.entities}
    relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in catalog.relations
    }

    assert catalog.source.publisher == "NEXON Korea"
    assert catalog.source.url.endswith("/Guide/GameInformation/Terms/Terms")
    assert entities["game-term:additional-option"].canonical_name == "추가 옵션"
    assert entities["abbreviation:additional-option"].canonical_name == "추옵"
    assert (
        "abbreviation:additional-option",
        "abbreviation_of",
        "game-term:additional-option",
    ) in relations
    assert entities["item:ark-innocence-scroll"].canonical_name == "아크 이노센트 주문서"
    assert entities["abbreviation:ark-innocence-scroll"].canonical_name == "아크이노"
    assert (
        "abbreviation:ark-innocence-scroll",
        "abbreviation_of",
        "item:ark-innocence-scroll",
    ) in relations
    assert (
        "item:ark-innocence-scroll",
        "has_effect",
        "effect:ark-innocence-reset",
    ) in relations


def test_full_boss_catalog_and_seasonal_meyrin_are_distinct_and_provenance_bearing() -> None:
    bosses = load_boss_catalog()
    meyrin = load_meyrin_boss_catalog()
    boss_entities = {entity.entity_id: entity for entity in bosses.entities}
    meyrin_entities = {entity.entity_id: entity for entity in meyrin.entities}
    boss_relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in bosses.relations
    }
    meyrin_relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in meyrin.relations
    }

    assert bosses.source.publisher == "NEXON Korea"
    assert "/Guide/N23GameInformation/Articles/Search" in bosses.source.url
    assert len([entity for entity in bosses.entities if entity.entity_type == "boss"]) == 33
    assert len([entity for entity in bosses.entities if entity.entity_type == "boss_variant"]) == 79
    assert boss_entities["boss:bellona"].entity_type == "boss"
    assert {
        entity.canonical_name
        for entity in bosses.entities
        if entity.entity_id.startswith("boss-variant:bellona-")
    } == {"벨로나 (이지)", "벨로나 (노멀)", "벨로나 (하드)"}
    assert (
        "boss-variant:bellona-normal",
        "variant_of",
        "boss:bellona",
    ) in boss_relations
    assert (
        "boss-variant:kaling-extreme",
        "has_difficulty",
        "difficulty:extreme",
    ) in boss_relations
    assert "boss-variant:black-mage-normal" not in boss_entities
    assert "boss:jupiter" in boss_entities

    assert meyrin.source.publisher == "NEXON Korea"
    assert meyrin.source.url.endswith("/News/Update/805")
    assert meyrin_entities["boss:meyrin"].entity_type == "boss"
    assert {
        entity.canonical_name for entity in meyrin.entities if entity.entity_type == "boss_variant"
    } == {"메이린 (노멀)", "메이린 (하드)"}
    assert (
        "boss-variant:meyrin-hard",
        "variant_of",
        "boss:meyrin",
    ) in meyrin_relations

    assert set(boss_entities).isdisjoint(meyrin_entities)


def test_content_catalogs_separate_union_champion_and_destiny_from_bosses() -> None:
    union = load_union_champion_catalog()
    destiny = load_destiny_weapon_catalog()
    union_entities = {entity.entity_id: entity for entity in union.entities}
    destiny_entities = {entity.entity_id: entity for entity in destiny.entities}
    union_relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in union.relations
    }
    destiny_relations = {
        (relation.subject_entity_id, relation.predicate, relation.object_entity_id)
        for relation in destiny.relations
    }

    assert union_entities["system:union-champion"].entity_type == "content_system"
    assert union_entities["abbreviation:union-champion"].canonical_name == "유챔"
    assert (
        "abbreviation:union-champion",
        "abbreviation_of",
        "system:union-champion",
    ) in union_relations

    assert destiny_entities["weapon:destiny"].entity_type == "weapon"
    assert destiny.source.publisher == "NEXON Korea"
    assert destiny.source.version.startswith("1.2.401")
    assert destiny_entities["content-mode:destiny"].entity_type == "content_mode"
    assert "데스티니 난이도" in destiny_entities["content-mode:destiny"].aliases
    assert destiny_entities["boss-variant:chosen-seren-destiny"].entity_type == "boss_variant"
    assert (
        "content-mode:destiny",
        "part_of",
        "progression:destiny-weapon",
    ) in destiny_relations
    assert (
        "boss-variant:chosen-seren-destiny",
        "variant_of",
        "boss:chosen-seren",
    ) in destiny_relations
    assert (
        "boss-variant:chosen-seren-destiny",
        "uses_mode",
        "content-mode:destiny",
    ) in destiny_relations
    assert (
        "boss-variant:chosen-seren-destiny",
        "has_condition",
        "challenge-condition:destiny-final-damage-80-down",
    ) in destiny_relations
    assert set(destiny.external_entity_ids) == {
        "boss:chosen-seren",
        "boss:watcher-kalos",
        "boss:kaling",
    }
