"""Validated loader for repository-managed knowledge graph catalogs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any


def normalize_alias(value: str) -> str:
    """Normalize a surface form without embedding domain-specific corrections."""

    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    title: str
    url: str
    publisher: str
    version: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class EntityDefinition:
    entity_id: str
    entity_type: str
    canonical_name: str
    description: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RelationDefinition:
    subject_entity_id: str
    predicate: str
    object_entity_id: str


@dataclass(frozen=True, slots=True)
class KnowledgeCatalog:
    schema_version: int
    source: SourceDefinition
    entities: tuple[EntityDefinition, ...]
    relations: tuple[RelationDefinition, ...]
    external_entity_ids: tuple[str, ...]
    content_hash: str


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"catalog field {key!r} must be a non-empty string")
    return value.strip()


def _load_catalog(filename: str) -> KnowledgeCatalog:
    resource = files("maple_chat.knowledge.data").joinpath(filename)
    raw = resource.read_bytes()
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("knowledge catalog root must be an object")
    schema_version = parsed.get("schema_version")
    if schema_version != 1:
        raise ValueError(f"unsupported knowledge catalog schema_version={schema_version!r}")

    source_raw = parsed.get("source")
    if not isinstance(source_raw, dict):
        raise ValueError("knowledge catalog source must be an object")
    source = SourceDefinition(
        source_id=_required_string(source_raw, "source_id"),
        title=_required_string(source_raw, "title"),
        url=_required_string(source_raw, "url"),
        publisher=_required_string(source_raw, "publisher"),
        version=_required_string(source_raw, "version"),
        checked_at=datetime.fromisoformat(_required_string(source_raw, "checked_at")),
    )
    if source.checked_at.tzinfo is None:
        raise ValueError("knowledge source checked_at must include a timezone")

    raw_ambiguous_aliases = parsed.get("allowed_ambiguous_aliases", [])
    if not isinstance(raw_ambiguous_aliases, list) or not all(
        isinstance(alias, str) and alias.strip() for alias in raw_ambiguous_aliases
    ):
        raise ValueError("allowed_ambiguous_aliases must contain non-empty strings")
    allowed_ambiguous_aliases = {normalize_alias(alias) for alias in raw_ambiguous_aliases}

    raw_external_ids = parsed.get("external_entity_ids", [])
    if not isinstance(raw_external_ids, list) or not all(
        isinstance(entity_id, str) and entity_id.strip() for entity_id in raw_external_ids
    ):
        raise ValueError("external_entity_ids must contain non-empty strings")
    external_entity_ids = tuple(dict.fromkeys(entity_id.strip() for entity_id in raw_external_ids))

    entities_raw = parsed.get("entities")
    if not isinstance(entities_raw, list) or not entities_raw:
        raise ValueError("knowledge catalog entities must be a non-empty list")
    entities: list[EntityDefinition] = []
    entity_ids: set[str] = set()
    normalized_aliases: dict[str, str] = {}
    for raw_entity in entities_raw:
        if not isinstance(raw_entity, dict):
            raise ValueError("each knowledge entity must be an object")
        entity_id = _required_string(raw_entity, "entity_id")
        if entity_id in entity_ids:
            raise ValueError(f"duplicate knowledge entity_id={entity_id}")
        entity_ids.add(entity_id)
        canonical_name = _required_string(raw_entity, "canonical_name")
        raw_aliases = raw_entity.get("aliases", [])
        if not isinstance(raw_aliases, list) or not all(
            isinstance(alias, str) and alias.strip() for alias in raw_aliases
        ):
            raise ValueError(f"aliases for {entity_id} must be non-empty strings")
        all_aliases = (canonical_name, *(alias.strip() for alias in raw_aliases))
        for alias in all_aliases:
            normalized = normalize_alias(alias)
            prior = normalized_aliases.setdefault(normalized, entity_id)
            if prior != entity_id and normalized not in allowed_ambiguous_aliases:
                raise ValueError(
                    f"ambiguous normalized alias={normalized!r} for {prior} and {entity_id}"
                )
        description = raw_entity.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"description for {entity_id} must be a string or null")
        entities.append(
            EntityDefinition(
                entity_id=entity_id,
                entity_type=_required_string(raw_entity, "entity_type"),
                canonical_name=canonical_name,
                description=description,
                aliases=tuple(alias.strip() for alias in raw_aliases),
            )
        )

    relations_raw = parsed.get("relations")
    if not isinstance(relations_raw, list) or not relations_raw:
        raise ValueError("knowledge catalog relations must be a non-empty list")
    relations: list[RelationDefinition] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for raw_relation in relations_raw:
        if not isinstance(raw_relation, dict):
            raise ValueError("each knowledge relation must be an object")
        relation = RelationDefinition(
            subject_entity_id=_required_string(raw_relation, "subject_entity_id"),
            predicate=_required_string(raw_relation, "predicate"),
            object_entity_id=_required_string(raw_relation, "object_entity_id"),
        )
        known_entity_ids = entity_ids | set(external_entity_ids)
        if relation.subject_entity_id not in known_entity_ids:
            raise ValueError(f"unknown relation subject={relation.subject_entity_id}")
        if relation.object_entity_id not in known_entity_ids:
            raise ValueError(f"unknown relation object={relation.object_entity_id}")
        key = (
            relation.subject_entity_id,
            relation.predicate,
            relation.object_entity_id,
        )
        if key in relation_keys:
            raise ValueError(f"duplicate knowledge relation={key}")
        relation_keys.add(key)
        relations.append(relation)

    return KnowledgeCatalog(
        schema_version=schema_version,
        source=source,
        entities=tuple(entities),
        relations=tuple(relations),
        external_entity_ids=external_entity_ids,
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def load_job_catalog() -> KnowledgeCatalog:
    return _load_catalog("job-taxonomy-v1.json")


def load_item_abbreviation_catalog() -> KnowledgeCatalog:
    return _load_catalog("item-abbreviations-v1.json")


def load_game_term_catalog() -> KnowledgeCatalog:
    return _load_catalog("game-terms-v1.json")


def load_boss_catalog() -> KnowledgeCatalog:
    return _load_catalog("boss-taxonomy-v1.json")


def load_meyrin_boss_catalog() -> KnowledgeCatalog:
    return _load_catalog("boss-meyrin-v1.json")


def load_union_champion_catalog() -> KnowledgeCatalog:
    return _load_catalog("union-champion-v1.json")


def load_destiny_weapon_catalog() -> KnowledgeCatalog:
    return _load_catalog("destiny-weapon-v1.json")


def load_builtin_catalogs() -> tuple[KnowledgeCatalog, ...]:
    return (
        load_job_catalog(),
        load_item_abbreviation_catalog(),
        load_game_term_catalog(),
        load_boss_catalog(),
        load_meyrin_boss_catalog(),
        load_union_champion_catalog(),
        load_destiny_weapon_catalog(),
    )
