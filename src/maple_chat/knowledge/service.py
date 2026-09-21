"""Database synchronization, entity linking, and graph traversal."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import (
    KnowledgeAlias,
    KnowledgeEntity,
    KnowledgeRelation,
    KnowledgeSource,
)
from maple_chat.knowledge.catalog import (
    KnowledgeCatalog,
    load_builtin_catalogs,
    load_job_catalog,
    normalize_alias,
)

_RETIRED_BUILTIN_SOURCE_IDS = ("nexon-bellona-update-1-2-418",)
_MAX_GRAPH_HOPS = 2
_MAX_GLOBAL_RELATIONS = 128


@dataclass(frozen=True, slots=True)
class KnowledgeSyncResult:
    sources: int
    entities: int
    aliases: int
    relations: int


@dataclass(frozen=True, slots=True)
class AliasRecord:
    alias: str
    entity_id: str
    entity_type: str


@dataclass(frozen=True, slots=True)
class EntityMatch:
    entity_id: str
    entity_type: str
    alias: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class KnowledgeFact:
    relation_id: str
    subject_entity_id: str
    subject_name: str
    subject_type: str
    predicate: str
    object_entity_id: str
    object_name: str
    object_type: str
    source_id: str
    source_title: str
    source_url: str
    source_version: str


def relation_id(subject: str, predicate: str, object_: str) -> str:
    payload = f"{subject}\0{predicate}\0{object_}".encode()
    return hashlib.sha256(payload).hexdigest()


def _expanded_relation_ids(
    seed_entity_ids: set[str],
    relations: tuple[KnowledgeRelation, ...],
    *,
    max_hops: int,
) -> set[str]:
    """Return a deterministic, bounded undirected neighborhood for global queries."""

    if max_hops < 1 or max_hops > _MAX_GRAPH_HOPS:
        raise ValueError(f"max_hops must be between 1 and {_MAX_GRAPH_HOPS}")

    selected: set[str] = set()
    visited_entities = set(seed_entity_ids)
    frontier = set(seed_entity_ids)
    for _ in range(max_hops):
        next_frontier: set[str] = set()
        for relation in relations:
            if (
                relation.subject_entity_id not in frontier
                and relation.object_entity_id not in frontier
            ):
                continue
            selected.add(relation.relation_id)
            next_frontier.update((relation.subject_entity_id, relation.object_entity_id))
            if len(selected) >= _MAX_GLOBAL_RELATIONS:
                return selected
        next_frontier.difference_update(visited_entities)
        if not next_frontier:
            break
        visited_entities.update(next_frontier)
        frontier = next_frontier
    return selected


def select_entity_matches(query: str, aliases: tuple[AliasRecord, ...]) -> tuple[EntityMatch, ...]:
    """Select longest non-overlapping catalog aliases from normalized user text."""

    normalized_query = normalize_alias(query)
    candidates: list[EntityMatch] = []
    for record in aliases:
        alias = normalize_alias(record.alias)
        start = normalized_query.find(alias)
        while start >= 0:
            candidates.append(
                EntityMatch(
                    entity_id=record.entity_id,
                    entity_type=record.entity_type,
                    alias=record.alias,
                    start=start,
                    end=start + len(alias),
                )
            )
            start = normalized_query.find(alias, start + 1)
    candidates.sort(key=lambda match: (-(match.end - match.start), match.start, match.entity_id))
    selected: list[EntityMatch] = []
    for candidate in candidates:
        overlapping = [
            existing
            for existing in selected
            if candidate.start < existing.end and existing.start < candidate.end
        ]
        if overlapping and not all(
            candidate.start == existing.start and candidate.end == existing.end
            for existing in overlapping
        ):
            continue
        if any(candidate.entity_id == existing.entity_id for existing in overlapping):
            continue
        selected.append(candidate)

    # Resolve specificity inside the job ontology only. A global maximum used to
    # discard a named boss whenever a job was also named because both belong to
    # independent ontology facets. Generic job words must still not expand every job.
    job_specificity = {
        "taxonomy": 1,
        "job_family": 2,
        "job": 3,
    }
    job_matches = [
        match
        for match in selected
        if match.entity_type in {"job", "job_family"} or match.entity_id == "taxonomy:jobs"
    ]
    if job_matches:
        highest_specificity = max(job_specificity[match.entity_type] for match in job_matches)
        selected = [
            match
            for match in selected
            if match.entity_type not in job_specificity
            or job_specificity[match.entity_type] == highest_specificity
        ]
    # A generic taxonomy word in a classification question (for example
    # "데스티니는 보스야?") must not expand every member of that taxonomy. The
    # concrete entity supplies the answerable type signal; taxonomy expansion is
    # reserved for taxonomy-only questions such as "전체 보스 난이도".
    if any(match.entity_type != "taxonomy" for match in selected):
        selected = [match for match in selected if match.entity_type != "taxonomy"]
    selected.sort(key=lambda match: (match.start, match.end, match.entity_id))
    return tuple(selected)


async def sync_knowledge_catalog(
    session: AsyncSession,
    catalog: KnowledgeCatalog,
    *,
    synced_at: datetime,
) -> KnowledgeSyncResult:
    source = catalog.source
    await session.execute(
        insert(KnowledgeSource)
        .values(
            source_id=source.source_id,
            title=source.title,
            url=source.url,
            publisher=source.publisher,
            version=source.version,
            checked_at=source.checked_at,
            content_hash=catalog.content_hash,
            active=True,
            updated_at=synced_at,
        )
        .on_conflict_do_update(
            index_elements=[KnowledgeSource.source_id],
            set_={
                "title": source.title,
                "url": source.url,
                "publisher": source.publisher,
                "version": source.version,
                "checked_at": source.checked_at,
                "content_hash": catalog.content_hash,
                "active": True,
                "updated_at": synced_at,
            },
        )
    )

    if catalog.external_entity_ids:
        available_external_ids = set(
            await session.scalars(
                sa.select(KnowledgeEntity.entity_id).where(
                    KnowledgeEntity.entity_id.in_(catalog.external_entity_ids),
                    KnowledgeEntity.active,
                )
            )
        )
        missing_external_ids = set(catalog.external_entity_ids) - available_external_ids
        if missing_external_ids:
            missing = ", ".join(sorted(missing_external_ids))
            raise ValueError(f"knowledge catalog external entities are unavailable: {missing}")

    entity_ids = [entity.entity_id for entity in catalog.entities]
    for entity in catalog.entities:
        await session.execute(
            insert(KnowledgeEntity)
            .values(
                entity_id=entity.entity_id,
                entity_type=entity.entity_type,
                canonical_name=entity.canonical_name,
                description=entity.description,
                source_id=source.source_id,
                metadata_json={},
                active=True,
                updated_at=synced_at,
            )
            .on_conflict_do_update(
                index_elements=[KnowledgeEntity.entity_id],
                set_={
                    "entity_type": entity.entity_type,
                    "canonical_name": entity.canonical_name,
                    "description": entity.description,
                    "source_id": source.source_id,
                    "active": True,
                    "updated_at": synced_at,
                },
            )
        )
    await session.execute(
        sa.update(KnowledgeEntity)
        .where(
            KnowledgeEntity.source_id == source.source_id,
            KnowledgeEntity.entity_id.not_in(entity_ids),
        )
        .values(active=False, updated_at=synced_at)
    )

    alias_keys: list[tuple[str, str]] = []
    alias_count = 0
    for entity in catalog.entities:
        surface_forms = (
            (entity.canonical_name, "canonical"),
            *((alias, "abbreviation") for alias in entity.aliases),
        )
        for alias, alias_type in surface_forms:
            normalized = normalize_alias(alias)
            alias_keys.append((normalized, entity.entity_id))
            alias_count += 1
            await session.execute(
                insert(KnowledgeAlias)
                .values(
                    normalized_alias=normalized,
                    entity_id=entity.entity_id,
                    alias=alias,
                    alias_type=alias_type,
                    source_id=source.source_id,
                    active=True,
                )
                .on_conflict_do_update(
                    index_elements=[KnowledgeAlias.normalized_alias, KnowledgeAlias.entity_id],
                    set_={
                        "alias": alias,
                        "alias_type": alias_type,
                        "source_id": source.source_id,
                        "active": True,
                    },
                )
            )
    existing_aliases = (
        await session.execute(
            sa.select(KnowledgeAlias.normalized_alias, KnowledgeAlias.entity_id).where(
                KnowledgeAlias.source_id == source.source_id
            )
        )
    ).all()
    active_alias_keys = set(alias_keys)
    for normalized, entity_id in existing_aliases:
        if (normalized, entity_id) not in active_alias_keys:
            await session.execute(
                sa.update(KnowledgeAlias)
                .where(
                    KnowledgeAlias.normalized_alias == normalized,
                    KnowledgeAlias.entity_id == entity_id,
                )
                .values(active=False)
            )

    relation_ids: list[str] = []
    for relation in catalog.relations:
        current_relation_id = relation_id(
            relation.subject_entity_id,
            relation.predicate,
            relation.object_entity_id,
        )
        relation_ids.append(current_relation_id)
        await session.execute(
            insert(KnowledgeRelation)
            .values(
                relation_id=current_relation_id,
                subject_entity_id=relation.subject_entity_id,
                predicate=relation.predicate,
                object_entity_id=relation.object_entity_id,
                source_id=source.source_id,
                version=source.version,
                metadata_json={},
                active=True,
                updated_at=synced_at,
            )
            .on_conflict_do_update(
                index_elements=[KnowledgeRelation.relation_id],
                set_={
                    "source_id": source.source_id,
                    "version": source.version,
                    "active": True,
                    "updated_at": synced_at,
                },
            )
        )
    await session.execute(
        sa.update(KnowledgeRelation)
        .where(
            KnowledgeRelation.source_id == source.source_id,
            KnowledgeRelation.relation_id.not_in(relation_ids),
        )
        .values(active=False, updated_at=synced_at)
    )
    return KnowledgeSyncResult(1, len(catalog.entities), alias_count, len(catalog.relations))


async def sync_builtin_job_catalog(
    session: AsyncSession, *, synced_at: datetime
) -> KnowledgeSyncResult:
    return await sync_knowledge_catalog(session, load_job_catalog(), synced_at=synced_at)


async def sync_builtin_knowledge(
    session: AsyncSession, *, synced_at: datetime
) -> KnowledgeSyncResult:
    results = [
        await sync_knowledge_catalog(session, catalog, synced_at=synced_at)
        for catalog in load_builtin_catalogs()
    ]
    await session.execute(
        sa.update(KnowledgeSource)
        .where(KnowledgeSource.source_id.in_(_RETIRED_BUILTIN_SOURCE_IDS))
        .values(active=False, updated_at=synced_at)
    )
    return KnowledgeSyncResult(
        sources=sum(result.sources for result in results),
        entities=sum(result.entities for result in results),
        aliases=sum(result.aliases for result in results),
        relations=sum(result.relations for result in results),
    )


class DatabaseKnowledgeRetriever:
    """Exact graph retriever; no vector similarity or answer post-processing."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def retrieve(self, query: str, *, max_hops: int = 1) -> tuple[KnowledgeFact, ...]:
        if max_hops < 1 or max_hops > _MAX_GRAPH_HOPS:
            raise ValueError(f"max_hops must be between 1 and {_MAX_GRAPH_HOPS}")
        async with self.factory() as session:
            alias_rows = (
                await session.execute(
                    sa.select(
                        KnowledgeAlias.alias,
                        KnowledgeAlias.entity_id,
                        KnowledgeEntity.entity_type,
                    )
                    .join(KnowledgeEntity, KnowledgeEntity.entity_id == KnowledgeAlias.entity_id)
                    .join(KnowledgeSource, KnowledgeSource.source_id == KnowledgeAlias.source_id)
                    .where(
                        KnowledgeAlias.active,
                        KnowledgeEntity.active,
                        KnowledgeSource.active,
                    )
                )
            ).all()
            aliases = tuple(AliasRecord(*row) for row in alias_rows)
            matches = select_entity_matches(query, aliases)
            if not matches:
                return ()

            relation_rows = (
                await session.execute(
                    sa.select(KnowledgeRelation)
                    .where(KnowledgeRelation.active)
                    .order_by(KnowledgeRelation.relation_id)
                )
            ).scalars()
            relations = tuple(relation_rows)

            selected: list[KnowledgeRelation] = []
            for match in matches:
                entity_id = match.entity_id
                if match.entity_type == "job":
                    selected.extend(
                        relation
                        for relation in relations
                        if relation.subject_entity_id == entity_id and relation.predicate == "is_a"
                    )
                elif match.entity_type == "job_family":
                    selected.extend(
                        relation
                        for relation in relations
                        if (relation.object_entity_id == entity_id and relation.predicate == "is_a")
                        or (
                            relation.subject_entity_id == entity_id
                            and relation.predicate == "part_of"
                        )
                    )
                elif match.entity_type == "taxonomy":
                    member_ids = {
                        relation.subject_entity_id
                        for relation in relations
                        if relation.object_entity_id == entity_id
                        and relation.predicate == "part_of"
                    }
                    selected.extend(
                        relation
                        for relation in relations
                        if (
                            relation.object_entity_id == entity_id
                            and relation.predicate == "part_of"
                        )
                        or (
                            relation.object_entity_id in member_ids
                            and relation.predicate in {"is_a", "abbreviation_of", "variant_of"}
                        )
                    )
                elif match.entity_type == "abbreviation":
                    direct_relations = [
                        relation
                        for relation in relations
                        if relation.subject_entity_id == entity_id
                        or relation.object_entity_id == entity_id
                    ]
                    selected.extend(direct_relations)
                    canonical_target_ids = {
                        relation.object_entity_id
                        for relation in direct_relations
                        if relation.subject_entity_id == entity_id
                        and relation.predicate == "abbreviation_of"
                    }
                    selected.extend(
                        relation
                        for relation in relations
                        if relation.subject_entity_id in canonical_target_ids
                        and relation.predicate == "has_effect"
                    )
                elif match.entity_type == "boss":
                    variant_ids = {
                        relation.subject_entity_id
                        for relation in relations
                        if relation.object_entity_id == entity_id
                        and relation.predicate == "variant_of"
                    }
                    selected.extend(
                        relation
                        for relation in relations
                        if relation.subject_entity_id == entity_id
                        or relation.object_entity_id == entity_id
                        or (
                            relation.subject_entity_id in variant_ids
                            and relation.predicate == "has_difficulty"
                        )
                    )
                else:
                    selected.extend(
                        relation
                        for relation in relations
                        if relation.subject_entity_id == entity_id
                        or relation.object_entity_id == entity_id
                    )

            expanded_ids = (
                _expanded_relation_ids(
                    {match.entity_id for match in matches},
                    relations,
                    max_hops=max_hops,
                )
                if max_hops > 1
                else set()
            )
            selected.extend(
                relation for relation in relations if relation.relation_id in expanded_ids
            )

            selected_ids = {relation.relation_id for relation in selected}
            if not selected_ids:
                return ()
            subject = sa.orm.aliased(KnowledgeEntity)
            object_ = sa.orm.aliased(KnowledgeEntity)
            rows = (
                await session.execute(
                    sa.select(KnowledgeRelation, subject, object_, KnowledgeSource)
                    .join(subject, subject.entity_id == KnowledgeRelation.subject_entity_id)
                    .join(object_, object_.entity_id == KnowledgeRelation.object_entity_id)
                    .join(
                        KnowledgeSource,
                        KnowledgeSource.source_id == KnowledgeRelation.source_id,
                    )
                    .where(
                        KnowledgeRelation.relation_id.in_(selected_ids),
                        KnowledgeRelation.active,
                        subject.active,
                        object_.active,
                        KnowledgeSource.active,
                    )
                    .order_by(
                        subject.entity_type,
                        subject.canonical_name,
                        KnowledgeRelation.predicate,
                    )
                )
            ).all()
            return tuple(
                KnowledgeFact(
                    relation_id=relation.relation_id,
                    subject_entity_id=subject_entity.entity_id,
                    subject_name=subject_entity.canonical_name,
                    subject_type=subject_entity.entity_type,
                    predicate=relation.predicate,
                    object_entity_id=object_entity.entity_id,
                    object_name=object_entity.canonical_name,
                    object_type=object_entity.entity_type,
                    source_id=source.source_id,
                    source_title=source.title,
                    source_url=source.url,
                    source_version=source.version,
                )
                for relation, subject_entity, object_entity, source in rows
            )
