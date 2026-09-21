"""Dense/lexical candidate retrieval, RRF, bounded boosts, and reranking."""

from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.db.models import Chunk, ChunkEmbedding, EmbeddingRevision
from maple_chat.knowledge.catalog import normalize_alias


@dataclass(frozen=True, slots=True)
class Candidate:
    chunk_id: str
    source_key: str
    text: str
    metadata: dict[str, Any]
    dense_score: float | None = None
    lexical_score: float | None = None
    fusion_score: float = 0.0
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: tuple[Candidate, ...]
    insufficient_evidence: bool


class Reranker(Protocol):
    async def score(self, query: str, documents: list[str]) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class RerankerSpec:
    model: str
    model_commit: str

    def validate(self) -> None:
        if self.model != "BAAI/bge-reranker-v2-m3" or not self.model_commit:
            raise ValueError("BGE reranker model and immutable commit are required")


class SentenceTransformerReranker:
    """Optional local BGE reranker adapter with an immutable, offline model revision."""

    def __init__(self, spec: RerankerSpec) -> None:
        spec.validate()
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            module = import_module("sentence_transformers")
        except ImportError as exc:  # pragma: no cover - optional production dependency
            raise RuntimeError("install the 'ml' optional dependency for local reranking") from exc
        self.spec = spec
        cross_encoder: Any = module.CrossEncoder
        self._model: Any = cross_encoder(
            spec.model,
            revision=spec.model_commit,
            local_files_only=True,
        )

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        pairs = [[query, document] for document in documents]
        scores = await asyncio.to_thread(self._model.predict, pairs)
        values = scores.tolist() if hasattr(scores, "tolist") else scores
        return [float(value) for value in values]


def _quality_boost(metadata: dict[str, Any], *, now: datetime) -> float:
    recommendations = max(0, int(metadata.get("recommendation_count", 0)))
    views = max(0, int(metadata.get("view_count", 0)))
    boost = min(0.004, math.log1p(recommendations) / 1000)
    boost += min(0.0025, math.log1p(views) / 4000)
    if metadata.get("category") in {"팁/정보", "팁과 노하우"}:
        boost += 0.0025
    if metadata.get("qa_branch"):
        boost += 0.0015
    published = metadata.get("published_at")
    if isinstance(published, str):
        try:
            age_days = max(
                0,
                (
                    now - datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(UTC)
                ).days,
            )
            boost += 0.003 * math.exp(-age_days / 365)
        except ValueError:
            pass
    community_boost = min(0.012, boost)
    official_boost = 0.02 if metadata.get("source_authority") == "official" else 0.0
    return community_boost + official_boost


def _anchor_boost(candidate: Candidate, anchor_terms: tuple[str, ...]) -> float:
    """Prefer exact graph-linked entities, especially in the source title."""

    if not anchor_terms:
        return 0.0
    title = normalize_alias(str(candidate.metadata.get("title") or ""))
    text = normalize_alias(candidate.text)
    authored_article = candidate.source_key.startswith("article:")
    boost = 0.0
    for term in anchor_terms:
        normalized = normalize_alias(term)
        if normalized in title or (authored_article and normalized in text):
            boost += 0.04
        elif normalized in text:
            boost += 0.006
    return min(0.06, boost)


def _matches_required_scope(
    candidate: Candidate, required_scope_groups: tuple[tuple[str, ...], ...]
) -> bool:
    """Require an entity match from every explicit query ontology facet."""

    if not required_scope_groups:
        return True
    searchable = normalize_alias(
        "\n".join((str(candidate.metadata.get("title") or ""), candidate.text))
    )
    return all(
        any(normalize_alias(term) in searchable for term in group)
        for group in required_scope_groups
    )


def _character_bigrams(value: str) -> set[str]:
    normalized = normalize_alias(value)
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def title_query_coverage(query: str, candidate: Candidate) -> float:
    """Measure how much of a query is covered by a short, high-signal title field."""

    query_bigrams = _character_bigrams(query)
    if not query_bigrams:
        return 0.0
    title_bigrams = _character_bigrams(str(candidate.metadata.get("title") or ""))
    return len(query_bigrams & title_bigrams) / len(query_bigrams)


def calibrated_rerank_score(query: str, candidate: Candidate, model_score: float) -> float:
    """Ensemble authored-article title relevance with the cross-encoder score."""

    if not candidate.source_key.startswith("article:"):
        return model_score
    return max(model_score, min(1.0, 0.6 * title_query_coverage(query, candidate)))


def reciprocal_rank_fusion(
    dense: list[Candidate],
    lexical: list[Candidate],
    *,
    now: datetime,
    anchor_terms: tuple[str, ...] = (),
    k: int = 60,
) -> list[Candidate]:
    combined: dict[str, Candidate] = {}
    scores: dict[str, float] = {}
    for candidates, score_field in ((dense, "dense_score"), (lexical, "lexical_score")):
        for rank, candidate in enumerate(candidates, start=1):
            prior = combined.get(candidate.chunk_id)
            if prior is None:
                combined[candidate.chunk_id] = candidate
            elif score_field == "lexical_score":
                combined[candidate.chunk_id] = replace(
                    prior,
                    metadata={**prior.metadata, **candidate.metadata},
                    lexical_score=candidate.lexical_score,
                )
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1 / (k + rank)
    fused = [
        replace(
            candidate,
            fusion_score=(
                scores[key]
                + _quality_boost(candidate.metadata, now=now)
                + _anchor_boost(candidate, anchor_terms)
            ),
        )
        for key, candidate in combined.items()
    ]
    return sorted(fused, key=lambda item: (-item.fusion_score, item.chunk_id))


async def _dense_candidates(
    factory: async_sessionmaker[AsyncSession], query_vector: list[float], limit: int
) -> list[Candidate]:
    distance = ChunkEmbedding.embedding.cosine_distance(query_vector).label("distance")
    async with factory() as session:
        rows = (
            await session.execute(
                sa.select(Chunk, distance)
                .join(ChunkEmbedding, ChunkEmbedding.chunk_id == Chunk.chunk_id)
                .join(EmbeddingRevision, EmbeddingRevision.id == ChunkEmbedding.revision_id)
                .where(
                    Chunk.active.is_(True),
                    ChunkEmbedding.active.is_(True),
                    EmbeddingRevision.active.is_(True),
                )
                .order_by(distance)
                .limit(limit)
            )
        ).all()
    return [
        Candidate(
            chunk_id=chunk.chunk_id,
            source_key=chunk.source_key,
            text=chunk.text,
            metadata=chunk.metadata_json,
            dense_score=max(0.0, 1.0 - float(value)),
        )
        for chunk, value in rows
    ]


async def _lexical_candidates(
    factory: async_sessionmaker[AsyncSession], query: str, limit: int
) -> list[Candidate]:
    similarity = sa.func.similarity(Chunk.text, query).label("similarity")
    async with factory() as session:
        rows = (
            await session.execute(
                sa.select(Chunk, similarity)
                .where(Chunk.active.is_(True), similarity > 0)
                .order_by(similarity.desc(), Chunk.chunk_id)
                .limit(limit)
            )
        ).all()
    return [
        Candidate(
            chunk_id=chunk.chunk_id,
            source_key=chunk.source_key,
            text=chunk.text,
            metadata=chunk.metadata_json,
            lexical_score=float(value),
        )
        for chunk, value in rows
    ]


async def hybrid_retrieve(
    factory: async_sessionmaker[AsyncSession],
    *,
    query: str,
    query_vector: list[float],
    reranker: Reranker,
    now: datetime,
    candidate_limit: int = 50,
    rerank_limit: int = 30,
    evidence_limit: int = 8,
    minimum_score: float = 0.15,
    anchor_terms: tuple[str, ...] = (),
    required_scope_groups: tuple[tuple[str, ...], ...] = (),
) -> RetrievalResult:
    if len(query_vector) != 1024:
        raise ValueError("query embedding dimension must be 1024")
    dense, lexical = await asyncio.gather(
        _dense_candidates(factory, query_vector, candidate_limit * 20),
        _lexical_candidates(factory, query, candidate_limit * 4),
    )
    from maple_chat.retrieval.evidence import evidence_group_key, rerank_document

    fused_candidates = reciprocal_rank_fusion(dense, lexical, now=now, anchor_terms=anchor_terms)
    fused: list[Candidate] = []
    group_counts: dict[str, int] = {}
    for candidate in fused_candidates:
        if not _matches_required_scope(candidate, required_scope_groups):
            continue
        group = evidence_group_key(candidate)
        if group_counts.get(group, 0) >= 3:
            continue
        fused.append(candidate)
        group_counts[group] = group_counts.get(group, 0) + 1
        if len(fused) >= rerank_limit:
            break
    if not fused:
        return RetrievalResult((), True)
    rerank_scores = await reranker.score(query, [rerank_document(candidate) for candidate in fused])
    if len(rerank_scores) != len(fused):
        raise ValueError("reranker returned the wrong score count")
    reranked = sorted(
        (
            replace(
                candidate,
                rerank_score=calibrated_rerank_score(query, candidate, score),
            )
            for candidate, score in zip(fused, rerank_scores, strict=True)
        ),
        key=lambda item: (-(item.rerank_score or 0.0), -item.fusion_score, item.chunk_id),
    )
    diverse: list[Candidate] = []
    seen_groups: set[str] = set()
    for candidate in reranked:
        score = candidate.rerank_score or 0.0
        group = evidence_group_key(candidate)
        if score < minimum_score or group in seen_groups:
            continue
        diverse.append(candidate)
        seen_groups.add(group)
        if len(diverse) >= evidence_limit:
            break
    return RetrievalResult(tuple(diverse), not diverse)


def classify_query_hints(query: str) -> dict[str, Any]:
    lowered = query.casefold()
    return {
        "test_server": any(token in lowered for token in ("테섭", "테스트 서버", "test server")),
        "live_server": any(token in lowered for token in ("본섭", "본 서버", "live server")),
        "as_of_requested": any(token in lowered for token in ("현재", "최신", "기준", "언제")),
    }
