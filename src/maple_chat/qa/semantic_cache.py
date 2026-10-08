"""Redis-backed semantic cache layer for high-frequency queries."""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import numpy as np
import redis.asyncio as aioredis
import sqlalchemy as sa
from redis.commands.search.field import TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
from redis.commands.search.query import Query
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from maple_chat.db.models import (
    Answer,
    AnswerKnowledgeSource,
    AnswerMode,
    AnswerSource,
)
from maple_chat.indexing.embedding import EmbeddingProvider
from maple_chat.knowledge.service import KnowledgeFact
from maple_chat.qa.service import (
    AnswerOutcome,
    AnswerProcessingMetrics,
    QAService,
    QuestionRequest,
    processing_metrics_footer,
)
from maple_chat.retrieval.hybrid import Candidate
from maple_chat.retrieval.modes import RAGQueryMode
from maple_chat.time import require_aware_utc, utc_now

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.96
DEFAULT_TTL_SECONDS = 7 * 86400  # 7 days
DEFAULT_INDEX_NAME = "maple_chat_semantic_cache"
DEFAULT_KEY_PREFIX = "maple_chat:cache:"
EMBEDDING_DIM = 1024


@dataclass(frozen=True, slots=True)
class CachedEntry:
    query: str
    text: str
    mode: AnswerMode
    sources: tuple[Candidate, ...]
    knowledge_facts: tuple[KnowledgeFact, ...]
    similarity: float
    created_at: datetime
    rag_mode: str = "mix"


def serialize_candidate(candidate: Candidate) -> dict[str, Any]:
    return {
        "chunk_id": candidate.chunk_id,
        "source_key": candidate.source_key,
        "text": candidate.text,
        "metadata": candidate.metadata,
        "dense_score": candidate.dense_score,
        "lexical_score": candidate.lexical_score,
        "fusion_score": candidate.fusion_score,
        "rerank_score": candidate.rerank_score,
    }


def deserialize_candidate(data: dict[str, Any]) -> Candidate:
    return Candidate(
        chunk_id=data["chunk_id"],
        source_key=data["source_key"],
        text=data["text"],
        metadata=data.get("metadata", {}),
        dense_score=data.get("dense_score"),
        lexical_score=data.get("lexical_score"),
        fusion_score=float(data.get("fusion_score", 0.0)),
        rerank_score=data.get("rerank_score"),
    )


def serialize_knowledge_fact(fact: KnowledgeFact) -> dict[str, Any]:
    return {
        "relation_id": fact.relation_id,
        "subject_entity_id": fact.subject_entity_id,
        "subject_name": fact.subject_name,
        "subject_type": fact.subject_type,
        "predicate": fact.predicate,
        "object_entity_id": fact.object_entity_id,
        "object_name": fact.object_name,
        "object_type": fact.object_type,
        "source_id": fact.source_id,
        "source_title": fact.source_title,
        "source_url": fact.source_url,
        "source_version": fact.source_version,
    }


def deserialize_knowledge_fact(data: dict[str, Any]) -> KnowledgeFact:
    return KnowledgeFact(
        relation_id=data["relation_id"],
        subject_entity_id=data["subject_entity_id"],
        subject_name=data["subject_name"],
        subject_type=data["subject_type"],
        predicate=data["predicate"],
        object_entity_id=data["object_entity_id"],
        object_name=data["object_name"],
        object_type=data["object_type"],
        source_id=data["source_id"],
        source_title=data["source_title"],
        source_url=data["source_url"],
        source_version=data["source_version"],
    )


class RedisSemanticCache:
    """Redis-backed vector semantic cache supporting RediSearch HNSW and standalone fallback."""

    def __init__(
        self,
        redis_url: str,
        *,
        index_name: str = DEFAULT_INDEX_NAME,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        client: aioredis.Redis | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.index_name = index_name
        self.key_prefix = key_prefix
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self._owns_client = client is None
        self.client: aioredis.Redis = client or aioredis.from_url(
            redis_url,
            decode_responses=False,
        )
        self._has_redisearch: bool | None = None
        self._index_initialized: bool = False

    async def ensure_index(self) -> bool:
        """Create RediSearch index if module is available; otherwise enable fallback."""
        if self._index_initialized:
            return bool(self._has_redisearch)
        try:
            # Check module availability
            has_search = False
            try:
                modules_raw = await self.client.execute_command("MODULE", "LIST")  # type: ignore[no-untyped-call]
                if isinstance(modules_raw, list):
                    for mod in modules_raw:
                        if (
                            isinstance(mod, list)
                            and len(mod) >= 2
                            and isinstance(mod[1], (bytes, str))
                        ):
                            name_val = mod[1]
                            name = (
                                name_val.decode("utf-8", errors="ignore").lower()
                                if isinstance(name_val, bytes)
                                else name_val.lower()
                            )
                            if "search" in name:
                                has_search = True
                                break
            except ResponseError:
                pass

            if not has_search:
                # Try FT._LIST in case modules command isn't standard
                try:
                    await self.client.execute_command("FT._LIST")  # type: ignore[no-untyped-call]
                    has_search = True
                except ResponseError:
                    has_search = False

            self._has_redisearch = has_search
            if not has_search:
                logger.info(
                    "RediSearch module not detected; "
                    "semantic cache will use standalone vector fallback"
                )
                self._index_initialized = True
                return False

            # Check if index already exists
            existing_indices = await self.client.execute_command("FT._LIST")  # type: ignore[no-untyped-call]
            existing_names: list[str] = []
            if isinstance(existing_indices, list):
                for idx in existing_indices:
                    existing_names.append(
                        idx.decode("utf-8") if isinstance(idx, bytes) else str(idx)
                    )

            if self.index_name in existing_names:
                self._index_initialized = True
                return True

            schema: list[Any] = [
                TextField("query"),
                VectorField(
                    "vector",
                    "HNSW",
                    {
                        "TYPE": "FLOAT32",
                        "DIM": EMBEDDING_DIM,
                        "DISTANCE_METRIC": "COSINE",
                    },
                ),
                TextField("text"),
                TextField("mode"),
                TagField("rag_mode"),
                TextField("sources_json"),
                TextField("knowledge_facts_json"),
                TextField("created_at"),
            ]
            definition = IndexDefinition(  # type: ignore[no-untyped-call]
                prefix=[self.key_prefix], index_type=IndexType.HASH
            )
            await self.client.ft(self.index_name).create_index(schema, definition=definition)
            self._index_initialized = True
            logger.info("RediSearch vector index '%s' created successfully", self.index_name)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to initialize RediSearch index, falling back to standalone scan: %s",
                exc,
            )
            self._has_redisearch = False
            self._index_initialized = True
            return False

    async def search(
        self,
        query_vector: Sequence[float],
        rag_mode: str = "mix",
    ) -> CachedEntry | None:
        """Search for the most similar cached query embedding."""
        if not self._index_initialized:
            await self.ensure_index()

        q_vec = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(q_vec)
        if norm > 0:
            q_vec = q_vec / norm

        if self._has_redisearch:
            return await self._search_redisearch(q_vec, rag_mode)
        return await self._search_fallback(q_vec, rag_mode)

    async def _search_redisearch(
        self,
        q_vec: np.ndarray,
        rag_mode: str,
    ) -> CachedEntry | None:
        try:
            vec_bytes = q_vec.tobytes()
            query = (
                Query("*=>[KNN 1 @vector $vec AS score]")
                .sort_by("score")
                .return_fields(
                    "query",
                    "text",
                    "mode",
                    "rag_mode",
                    "sources_json",
                    "knowledge_facts_json",
                    "created_at",
                    "score",
                )
                .dialect(2)
            )
            res = await self.client.ft(self.index_name).search(
                query, query_params={"vec": vec_bytes}
            )
            if not res or not res.docs:
                return None
            doc = res.docs[0]
            distance = float(doc.score)
            similarity = 1.0 - distance
            if similarity < self.similarity_threshold:
                return None

            return self._doc_to_entry(doc, similarity)
        except Exception as exc:
            logger.warning("RediSearch vector search failed: %s; trying fallback", exc)
            return await self._search_fallback(q_vec, rag_mode)

    async def _search_fallback(
        self,
        q_vec: np.ndarray,
        rag_mode: str,
    ) -> CachedEntry | None:
        try:
            cursor = 0
            best_entry: CachedEntry | None = None
            highest_sim = -1.0

            pattern = f"{self.key_prefix}*".encode()
            while True:
                cursor, keys = await self.client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    pipe = self.client.pipeline()
                    for k in keys:
                        pipe.hgetall(k)
                    results = await pipe.execute()
                    for data in results:
                        if not data or not isinstance(data, dict):
                            continue
                        vec_raw = data.get(b"vector")
                        if not vec_raw or len(vec_raw) != EMBEDDING_DIM * 4:
                            continue
                        stored_vec = np.frombuffer(vec_raw, dtype=np.float32)
                        stored_norm = np.linalg.norm(stored_vec)
                        if stored_norm > 0:
                            stored_vec = stored_vec / stored_norm
                        sim = float(np.dot(q_vec, stored_vec))
                        if sim > highest_sim:
                            highest_sim = sim
                            if sim >= self.similarity_threshold:
                                best_entry = self._data_to_entry(data, sim)
                if cursor == 0:
                    break

            if highest_sim >= self.similarity_threshold and best_entry is not None:
                return best_entry
            return None
        except Exception as exc:
            logger.warning("Fallback semantic cache search failed: %s", exc)
            return None

    def _doc_to_entry(self, doc: Any, similarity: float) -> CachedEntry:
        def _get_field(attr: str) -> str:
            val = getattr(doc, attr, None)
            if isinstance(val, bytes):
                return val.decode("utf-8")
            return str(val) if val is not None else ""

        query_str = _get_field("query")
        text_str = _get_field("text")
        mode_str = _get_field("mode")
        rag_mode_str = _get_field("rag_mode") or "mix"
        sources_json = _get_field("sources_json")
        facts_json = _get_field("knowledge_facts_json")
        created_at_str = _get_field("created_at")

        sources = tuple(
            deserialize_candidate(item)
            for item in json.loads(sources_json)
        ) if sources_json else ()

        facts = tuple(
            deserialize_knowledge_fact(item)
            for item in json.loads(facts_json)
        ) if facts_json else ()

        try:
            created_at = require_aware_utc(datetime.fromisoformat(created_at_str))
        except (ValueError, TypeError):
            created_at = utc_now()

        mode = (
            AnswerMode(mode_str)
            if mode_str in AnswerMode._value2member_map_
            else AnswerMode.GENERATED
        )
        return CachedEntry(
            query=query_str,
            text=text_str,
            mode=mode,
            sources=sources,
            knowledge_facts=facts,
            similarity=similarity,
            created_at=created_at,
            rag_mode=rag_mode_str,
        )

    def _data_to_entry(self, data: dict[bytes, bytes], similarity: float) -> CachedEntry:
        def _get_field(key: bytes) -> str:
            val = data.get(key)
            return val.decode("utf-8") if val is not None else ""

        query_str = _get_field(b"query")
        text_str = _get_field(b"text")
        mode_str = _get_field(b"mode")
        rag_mode_str = _get_field(b"rag_mode") or "mix"
        sources_json = _get_field(b"sources_json")
        facts_json = _get_field(b"knowledge_facts_json")
        created_at_str = _get_field(b"created_at")

        sources = tuple(
            deserialize_candidate(item)
            for item in json.loads(sources_json)
        ) if sources_json else ()

        facts = tuple(
            deserialize_knowledge_fact(item)
            for item in json.loads(facts_json)
        ) if facts_json else ()

        try:
            created_at = require_aware_utc(datetime.fromisoformat(created_at_str))
        except (ValueError, TypeError):
            created_at = utc_now()

        mode = (
            AnswerMode(mode_str)
            if mode_str in AnswerMode._value2member_map_
            else AnswerMode.GENERATED
        )
        return CachedEntry(
            query=query_str,
            text=text_str,
            mode=mode,
            sources=sources,
            knowledge_facts=facts,
            similarity=similarity,
            created_at=created_at,
            rag_mode=rag_mode_str,
        )

    async def store(
        self,
        *,
        query: str,
        query_vector: Sequence[float],
        text: str,
        mode: AnswerMode,
        sources: Sequence[Candidate],
        knowledge_facts: Sequence[KnowledgeFact],
        rag_mode: str = "mix",
    ) -> str:
        """Store verified answer and embedding in Redis with TTL."""
        try:
            doc_key = f"{self.key_prefix}{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
            vec = np.asarray(query_vector, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm

            sources_json = json.dumps(
                [serialize_candidate(c) for c in sources], ensure_ascii=False
            )
            facts_json = json.dumps(
                [serialize_knowledge_fact(f) for f in knowledge_facts], ensure_ascii=False
            )
            created_at_iso = utc_now().isoformat()

            mapping: dict[bytes, bytes] = {
                b"query": query.encode("utf-8"),
                b"vector": vec.tobytes(),
                b"text": text.encode("utf-8"),
                b"mode": mode.value.encode("utf-8"),
                b"rag_mode": rag_mode.encode("utf-8"),
                b"sources_json": sources_json.encode("utf-8"),
                b"knowledge_facts_json": facts_json.encode("utf-8"),
                b"created_at": created_at_iso.encode("utf-8"),
            }

            pipe = self.client.pipeline()
            cast(Any, pipe).hset(doc_key, mapping=mapping)
            pipe.expire(doc_key, self.ttl_seconds)
            await pipe.execute()
            logger.debug(
                "Stored semantic cache entry for query: %s (key: %s, ttl: %ds)",
                query,
                doc_key,
                self.ttl_seconds,
            )
            return doc_key
        except Exception as exc:
            logger.warning("Failed to store semantic cache entry: %s", exc)
            return ""

    async def invalidate_all(self) -> int:
        """Delete all cached semantic queries."""
        try:
            count = 0
            cursor = 0
            pattern = f"{self.key_prefix}*".encode()
            while True:
                cursor, keys = await self.client.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    deleted = await self.client.delete(*keys)
                    count += int(deleted)
                if cursor == 0:
                    break
            logger.info("Invalidated %d semantic cache entries", count)
            return count
        except Exception as exc:
            logger.error("Failed to invalidate semantic cache: %s", exc)
            return 0

    async def invalidate_on_patch(self, patch_version: str | None = None) -> int:
        """Invalidate cache entries on game patch deployment."""
        logger.info("Invalidating semantic cache on patch event (version=%s)", patch_version)
        return await self.invalidate_all()

    async def delete_by_query(self, query: str) -> bool:
        """Invalidate cache entry for a specific query."""
        try:
            doc_key = f"{self.key_prefix}{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
            res = await self.client.delete(doc_key)
            return bool(res > 0)
        except Exception as exc:
            logger.warning("Failed to delete cache entry for query '%s': %s", query, exc)
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def close(self) -> None:
        await self.aclose()


class SemanticCacheMiddleware:
    """Middleware intercepting QA pipeline with Redis semantic vector cache."""

    def __init__(
        self,
        service: QAService,
        cache: RedisSemanticCache,
        embedding: EmbeddingProvider,
        *,
        enabled: bool = True,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        source_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.service = service
        self.cache = cache
        self.embedding = embedding
        self.enabled = enabled
        self.similarity_threshold = similarity_threshold
        self.source_ttl = source_ttl

    async def answer(
        self,
        session: AsyncSession,
        request: QuestionRequest,
    ) -> AnswerOutcome:
        started_at = time.perf_counter()

        # Fail-safe / bypass check
        if not self.enabled or not request.rag_enabled or request.rag_mode is RAGQueryMode.BYPASS:
            return await self.service.answer(session, request)

        # Idempotency lock on request message id
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock(request.request_message_id)))
        existing = await session.scalar(
            sa.select(Answer).where(Answer.request_message_id == request.request_message_id)
        )
        if existing is not None:
            return AnswerOutcome(
                answer_id=existing.answer_id,
                mode=existing.mode,
                text="",
                sources=(),
                knowledge_facts=(),
                duplicate=True,
            )

        # Step 1: Embed user query (BGE-M3)
        cached_entry: CachedEntry | None = None
        query_vector: list[float] | None = None
        embedding_seconds: float | None = None
        try:
            embed_start = time.perf_counter()
            vectors = await self.embedding.embed([request.query])
            embedding_seconds = time.perf_counter() - embed_start
            if vectors:
                query_vector = vectors[0]
                # Step 2: Search Redis Vector similarity index
                cached_entry = await self.cache.search(
                    query_vector,
                    rag_mode=request.rag_mode.value,
                )
        except Exception as exc:
            logger.warning(
                "Semantic cache lookup failed, falling back to LightRAG pipeline: %s",
                exc,
            )
            cached_entry = None

        # Step 3: Cache Hit evaluation (threshold >= 0.96)
        if cached_entry is not None and cached_entry.similarity >= self.similarity_threshold:
            hit_total_seconds = time.perf_counter() - started_at
            logger.info(
                "semantic_cache_hit",
                extra={
                    "query": request.query,
                    "matched_query": cached_entry.query,
                    "similarity": cached_entry.similarity,
                    "total_seconds": hit_total_seconds,
                },
            )

            # Persist answer record for Discord buttons and audit trail
            persistence_started = time.perf_counter()
            answer_id = str(uuid.uuid4())
            session.add(
                Answer(
                    answer_id=answer_id,
                    guild_id=request.guild_id,
                    channel_id=request.channel_id,
                    request_message_id=request.request_message_id,
                    requester_hash=request.requester_hash,
                    query_hash=hashlib.sha256(request.query.encode()).hexdigest(),
                    mode=cached_entry.mode,
                    created_at=request.received_at,
                    expires_at=request.received_at + self.source_ttl,
                )
            )
            await session.flush()
            for rank, candidate in enumerate(cached_entry.sources, start=1):
                session.add(
                    AnswerSource(
                        answer_id=answer_id,
                        chunk_id=candidate.chunk_id,
                        retrieval_score=candidate.fusion_score,
                        rerank_score=candidate.rerank_score,
                        rank=rank,
                    )
                )
            for rank, fact in enumerate(cached_entry.knowledge_facts, start=1):
                session.add(
                    AnswerKnowledgeSource(
                        answer_id=answer_id,
                        relation_id=fact.relation_id,
                        rank=rank,
                    )
                )
            await session.flush()
            persistence_seconds = time.perf_counter() - persistence_started

            metrics = AnswerProcessingMetrics(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                input_tokens_per_second=None,
                output_tokens_per_second=None,
                model_seconds=0.0,
                total_seconds=hit_total_seconds,
                model_calls=0,
                embedding_seconds=embedding_seconds,
                persistence_seconds=persistence_seconds,
                cache_hit=True,
                cache_similarity=cached_entry.similarity,
            )
            text = cached_entry.text
            text = f"{text.rstrip()}\n\n{processing_metrics_footer(metrics)}"
            return AnswerOutcome(
                answer_id=answer_id,
                mode=cached_entry.mode,
                text=text,
                sources=cached_entry.sources,
                knowledge_facts=cached_entry.knowledge_facts,
                metrics=metrics,
                cached=True,
            )

        # Step 4: Cache Miss - Safe Fallback to full LightRAG pipeline
        outcome = await self.service.answer(session, request)

        # Store generated answer in cache for future high-frequency queries
        if (
            outcome.mode == AnswerMode.GENERATED
            and not outcome.duplicate
            and query_vector is not None
        ):
            try:
                clean_text = outcome.text
                if "\n\n---\n처리 지표:" in clean_text:
                    clean_text = clean_text.split("\n\n---\n처리 지표:")[0].rstrip()
                await self.cache.store(
                    query=request.query,
                    query_vector=query_vector,
                    text=clean_text,
                    mode=outcome.mode,
                    sources=outcome.sources,
                    knowledge_facts=outcome.knowledge_facts,
                    rag_mode=request.rag_mode.value,
                )
            except Exception as exc:
                logger.warning("Failed to store answer in semantic cache: %s", exc)

        return outcome
