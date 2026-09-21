"""Dense/lexical candidate retrieval, RRF, bounded boosts, and reranking."""

from __future__ import annotations

import asyncio
import math
import os
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, ClassVar, Protocol, cast
from urllib.parse import urlparse

import httpx
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


_RERANK_TOKENIZER_CACHE: dict[tuple[str, str], Any] = {}


def _load_rerank_tokenizer(model: str, revision: str) -> Any:
    """Load the pinned tokenizer offline for client-side document truncation."""

    cached = _RERANK_TOKENIZER_CACHE.get((model, revision))
    if cached is not None:
        return cached
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    module = import_module("transformers")
    tokenizer = module.AutoTokenizer.from_pretrained(
        model, revision=revision, local_files_only=True
    )
    _RERANK_TOKENIZER_CACHE[(model, revision)] = tokenizer
    return tokenizer


class RemoteReranker:
    """Approved-local always-on BGE-reranker-v2-m3 HTTP client (TEI/vLLM compatible).

    TEI 1.9 serves rerank requests at the origin-root ``/rerank`` path and
    returns 404 for ``/v1/rerank``, so the rerank and ``/info`` attestation
    URLs are pinned to the origin root regardless of any service path
    (for example ``/v1``) carried by ``base_url``.
    """

    _ALLOWED_HOSTS: ClassVar[set[str]] = {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.docker.internal",
        "dcm-reranker",
    }
    _MAX_ATTEMPTS: ClassVar[int] = 3
    _BACKOFF_BASE_SECONDS: ClassVar[float] = 0.25
    _BACKOFF_CAP_SECONDS: ClassVar[float] = 2.0
    _RETRYABLE_STATUS_CODES: ClassVar[frozenset[int]] = frozenset({429, 503})
    # bge-reranker-v2-m3 caps sequences at 512 learned positions. TEI encodes the
    # pair as [CLS] query [SEP] document [SEP] (3 special tokens) and only
    # truncates at its own max_input_length (8192), so long documents are billed
    # for positions the model cannot attend to. 480 keeps the document plus a
    # typical short query (measured 14-29 tokens) inside the 512-position window.
    _MAX_RERANK_DOCUMENT_TOKENS: ClassVar[int] = 480

    def __init__(
        self,
        spec: RerankerSpec,
        *,
        base_url: str,
        served_model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        tokenizer_loader: Callable[[], Any] | None = None,
    ) -> None:
        spec.validate()
        if not served_model.strip():
            raise ValueError("reranker served model must not be empty")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in self._ALLOWED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("reranker base URL must target the approved local model boundary")
        self.spec = spec
        self._served_model = served_model
        self._info_url = parsed._replace(path="/info").geturl()
        self._rerank_url = parsed._replace(path="/rerank").geturl()
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=timeout_seconds,
        )
        self._attestation_lock = asyncio.Lock()
        self._attested = False
        self._tokenizer_loader = tokenizer_loader or (
            lambda: _load_rerank_tokenizer(spec.model, spec.model_commit)
        )
        self._tokenizer: Any = None
        self._tokenizer_unavailable = False

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        """Send a request, retrying bounded TEI overload responses (429/503).

        Exhaustion deliberately returns the final response so callers keep the
        existing ``raise_for_status`` failure semantics that the no-rerank
        fail-open path relies on.
        """
        attempt = 1
        while True:
            request = self._client.build_request(
                method,
                url,
                json=json_body,
                timeout=self._timeout_seconds,
            )
            response = await self._client.send(request)
            if (
                response.status_code not in self._RETRYABLE_STATUS_CODES
                or attempt >= self._MAX_ATTEMPTS
            ):
                return response
            await asyncio.sleep(self._retry_delay(attempt, response))
            attempt += 1

    def _retry_delay(self, attempt: int, response: httpx.Response) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                requested = float(retry_after)
            except ValueError:
                requested = -1.0
            if requested >= 0 and math.isfinite(requested):
                return min(requested, self._BACKOFF_CAP_SECONDS)
        base = min(
            self._BACKOFF_CAP_SECONDS,
            self._BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
        )
        jitter = 0.5 + random.random() / 2  # noqa: S311 - backoff jitter, not crypto
        return float(base * jitter)

    async def _ensure_attested(self) -> None:
        """Attest the served model once per process and cache the verdict."""

        async with self._attestation_lock:
            if self._attested:
                return
            response = await self._request_with_retry("GET", self._info_url)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError("reranker service /info returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("reranker service /info returned an invalid response")
            if payload.get("model_id") != self.spec.model:
                raise RuntimeError("reranker service /info model_id does not match")
            if payload.get("model_sha") != self.spec.model_commit:
                raise RuntimeError("reranker service /info model_sha does not match")
            self._attested = True

    def _truncate_documents(self, documents: list[str]) -> list[str]:
        if self._tokenizer_unavailable:
            return documents
        tokenizer = self._tokenizer
        if tokenizer is None:
            try:
                tokenizer = self._tokenizer_loader()
            except Exception:  # truncation is a latency-only optimization; degrade untruncated
                self._tokenizer_unavailable = True
                return documents
            self._tokenizer = tokenizer
        max_tokens = self._MAX_RERANK_DOCUMENT_TOKENS
        truncated: list[str] = []
        for document in documents:
            encoded = tokenizer(
                document,
                truncation=True,
                max_length=max_tokens,
                add_special_tokens=False,
            )
            truncated.append(tokenizer.decode(encoded["input_ids"]))
        return truncated

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        await self._ensure_attested()
        texts = await asyncio.to_thread(self._truncate_documents, documents)
        response = await self._request_with_retry(
            "POST",
            self._rerank_url,
            json_body={
                "model": self._served_model,
                "query": query,
                "texts": texts,
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("reranker service returned invalid JSON") from exc
        if isinstance(payload, dict):
            response_model = payload.get("model")
            if response_model is not None and response_model != self._served_model:
                raise RuntimeError("reranker service model does not match the configured revision")
            results = payload.get("results")
        else:
            results = payload
        if not isinstance(results, list) or len(results) != len(documents):
            raise RuntimeError("reranker service returned the wrong score count")
        scores: list[float | None] = [None] * len(documents)
        for item in results:
            if not isinstance(item, dict):
                raise RuntimeError("reranker service returned an invalid score entry")
            index = item.get("index")
            raw_score = item.get("relevance_score", item.get("score"))
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(documents)
                or scores[index] is not None
            ):
                raise RuntimeError("reranker service returned invalid score ordering")
            if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
                raise RuntimeError("reranker service returned non-finite scores")
            try:
                value = float(raw_score)
            except (OverflowError, ValueError) as exc:
                raise RuntimeError("reranker service returned non-finite scores") from exc
            if not math.isfinite(value):
                raise RuntimeError("reranker service returned non-finite scores")
            scores[index] = value
        if any(score is None for score in scores):
            raise RuntimeError("reranker service returned incomplete scores")
        return [cast(float, score) for score in scores]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


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
