"""Read-only corpus audit for terminology drift and expansion candidates."""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import Chunk, KnowledgeEntity, KnowledgeRelation
from maple_chat.knowledge.catalog import normalize_alias


@dataclass(frozen=True, slots=True)
class ExpansionCandidate:
    candidate: str
    source_key: str
    chunk_id: str


@dataclass(frozen=True, slots=True)
class SurfaceFormCount:
    surface: str
    chunks: int


@dataclass(frozen=True, slots=True)
class TermAudit:
    abbreviation: str
    official_name: str
    abbreviation_chunks: int
    official_name_chunks: int
    matching_expansions: int
    different_expansions: tuple[ExpansionCandidate, ...]
    observed_compounds: tuple[SurfaceFormCount, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeAuditResult:
    checked_at: datetime
    active_chunks: int
    audited_terms: int
    different_expansion_candidates: int
    terms: tuple[TermAudit, ...]


def _decode_text(value: str) -> str:
    # Some historical comments contain nested HTML entity escaping.
    for _ in range(2):
        decoded = html.unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def expansion_candidates(text: str, abbreviation: str) -> tuple[str, ...]:
    """Extract only explicit parenthetical/equality expansions for human review."""

    decoded = _decode_text(text)
    escaped = re.escape(abbreviation)
    patterns = (
        re.compile(rf"{escaped}\s*[\(\[]\s*([^\)\]\n]{{2,60}})\s*[\)\]]"),
        re.compile(rf"{escaped}\s*=\s*([^,.;\n]{{2,60}})"),
    )
    values: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(decoded):
            candidate = " ".join(match.group(1).split())
            if candidate not in values:
                values.append(candidate)
    return tuple(values)


def compound_surfaces(text: str, abbreviation: str) -> tuple[str, ...]:
    """Collect attached community forms without deciding that they are aliases."""

    decoded = _decode_text(text)
    surfaces: list[str] = []
    for match in re.finditer(r"(?<![가-힣])([가-힣]{2,20})(?![가-힣])", decoded):
        surface = match.group(1)
        if abbreviation in surface and surface != abbreviation:
            surfaces.append(surface)
    return tuple(surfaces)


def _is_matching_expansion(candidate: str, official_name: str) -> bool:
    candidate_normalized = normalize_alias(candidate).replace(" ", "")
    official_normalized = normalize_alias(official_name).replace(" ", "")
    return official_normalized in candidate_normalized


def _looks_like_name_expansion(candidate: str, abbreviation: str, official_name: str) -> bool:
    candidate_normalized = normalize_alias(candidate).replace(" ", "")
    abbreviation_normalized = normalize_alias(abbreviation).replace(" ", "")
    official_normalized = normalize_alias(official_name).replace(" ", "")
    if "주문서" in candidate_normalized or official_normalized in candidate_normalized:
        return True
    if len(candidate_normalized) > max(12, len(abbreviation_normalized) + 6):
        return False
    similarity = SequenceMatcher(
        None, candidate_normalized, abbreviation_normalized, autojunk=False
    ).ratio()
    return similarity >= 0.55


async def audit_knowledge_terms(
    session: AsyncSession, *, checked_at: datetime
) -> KnowledgeAuditResult:
    abbreviation_entity = sa.orm.aliased(KnowledgeEntity)
    official_entity = sa.orm.aliased(KnowledgeEntity)
    rows = (
        await session.execute(
            sa.select(abbreviation_entity, official_entity)
            .join(
                KnowledgeRelation,
                KnowledgeRelation.subject_entity_id == abbreviation_entity.entity_id,
            )
            .join(
                official_entity,
                official_entity.entity_id == KnowledgeRelation.object_entity_id,
            )
            .where(
                KnowledgeRelation.predicate == "abbreviation_of",
                KnowledgeRelation.active,
                abbreviation_entity.active,
                official_entity.active,
            )
            .order_by(abbreviation_entity.canonical_name)
        )
    ).all()
    active_chunks = int(
        await session.scalar(sa.select(sa.func.count()).select_from(Chunk).where(Chunk.active)) or 0
    )
    terms: list[TermAudit] = []
    for abbreviation_row, official_row in rows:
        abbreviation = abbreviation_row.canonical_name
        official_name = official_row.canonical_name
        chunks = (
            await session.execute(
                sa.select(Chunk.chunk_id, Chunk.source_key, Chunk.text).where(
                    Chunk.active,
                    Chunk.text.contains(abbreviation),
                )
            )
        ).all()
        official_count = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(Chunk)
                .where(Chunk.active, Chunk.text.contains(official_name))
            )
            or 0
        )
        matching_expansions = 0
        different: list[ExpansionCandidate] = []
        compound_chunk_ids: dict[str, set[str]] = {}
        for chunk_id, source_key, text in chunks:
            for candidate in expansion_candidates(text, abbreviation):
                if not _looks_like_name_expansion(candidate, abbreviation, official_name):
                    continue
                if _is_matching_expansion(candidate, official_name):
                    matching_expansions += 1
                elif len(different) < 20:
                    different.append(ExpansionCandidate(candidate, source_key, chunk_id))
            for surface in set(compound_surfaces(text, abbreviation)):
                compound_chunk_ids.setdefault(surface, set()).add(chunk_id)
        compound_counts = Counter(
            {surface: len(chunk_ids) for surface, chunk_ids in compound_chunk_ids.items()}
        )
        observed_compounds = tuple(
            SurfaceFormCount(surface, count) for surface, count in compound_counts.most_common(20)
        )
        terms.append(
            TermAudit(
                abbreviation=abbreviation,
                official_name=official_name,
                abbreviation_chunks=len(chunks),
                official_name_chunks=official_count,
                matching_expansions=matching_expansions,
                different_expansions=tuple(different),
                observed_compounds=observed_compounds,
            )
        )
    return KnowledgeAuditResult(
        checked_at=checked_at,
        active_chunks=active_chunks,
        audited_terms=len(terms),
        different_expansion_candidates=sum(len(term.different_expansions) for term in terms),
        terms=tuple(terms),
    )
