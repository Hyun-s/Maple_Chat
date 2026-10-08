from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from redis.exceptions import ResponseError

from maple_chat.db.models import Answer, AnswerKnowledgeSource, AnswerMode, AnswerSource
from maple_chat.indexing.embedding import EmbeddingSpec
from maple_chat.knowledge.service import KnowledgeFact
from maple_chat.qa.semantic_cache import (
    RedisSemanticCache,
    SemanticCacheMiddleware,
    deserialize_candidate,
    deserialize_knowledge_fact,
    serialize_candidate,
    serialize_knowledge_fact,
)
from maple_chat.qa.service import (
    AnswerOutcome,
    AnswerProcessingMetrics,
    QAService,
    QuestionRequest,
    processing_metrics_footer,
)
from maple_chat.retrieval.hybrid import Candidate


def _to_bytes(val: Any) -> bytes:
    if isinstance(val, bytes):
        return val
    if isinstance(val, str):
        return val.encode("utf-8")
    return bytes(val)


class FakeRedis:
    def __init__(self, has_redisearch: bool = True) -> None:
        self.store: dict[bytes, dict[bytes, bytes]] = {}
        self.ttls: dict[bytes, int] = {}
        self.has_redisearch = has_redisearch
        self._indices: list[bytes] = []

    async def execute_command(self, *args: Any) -> Any:
        cmd = str(args[0]).upper()
        if cmd == "MODULE" and len(args) > 1 and str(args[1]).upper() == "LIST":
            if self.has_redisearch:
                return [[b"name", b"search", b"ver", 20800]]
            return []
        if cmd == "FT._LIST":
            if self.has_redisearch:
                return self._indices
            raise ResponseError("unknown command FT._LIST")
        return None

    def ft(self, index_name: str) -> FakeSearch:
        return FakeSearch(self, index_name)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def scan(
        self, cursor: int = 0, match: bytes | str | None = None, count: int = 100
    ) -> tuple[int, list[bytes]]:
        prefix = _to_bytes(match).rstrip(b"*") if match else b""
        matching = [k for k in self.store if k.startswith(prefix)]
        return 0, matching

    async def delete(self, *keys: Any) -> int:
        deleted = 0
        for k in keys:
            kb = _to_bytes(k)
            if kb in self.store:
                del self.store[kb]
                deleted += 1
            self.ttls.pop(kb, None)
        return deleted

    async def aclose(self) -> None:
        pass


class FakeSearch:
    def __init__(self, redis: FakeRedis, index_name: str) -> None:
        self.redis = redis
        self.index_name = _to_bytes(index_name)

    async def create_index(self, schema: Any, definition: Any) -> None:
        if self.index_name not in self.redis._indices:
            self.redis._indices.append(self.index_name)

    async def search(self, query: Any, query_params: dict[str, Any] | None = None) -> Any:
        if not query_params or "vec" not in query_params:
            return None
        q_vec = np.frombuffer(query_params["vec"], dtype=np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm

        best_doc = None
        best_sim = -1.0
        for doc_key, data in self.redis.store.items():
            stored_vec_bytes = data.get(b"vector")
            if not stored_vec_bytes:
                continue
            stored_vec = np.frombuffer(stored_vec_bytes, dtype=np.float32)
            s_norm = np.linalg.norm(stored_vec)
            if s_norm > 0:
                stored_vec = stored_vec / s_norm
            sim = float(np.dot(q_vec, stored_vec))
            if sim > best_sim:
                best_sim = sim
                dist = 1.0 - sim

                best_doc = SimpleNamespace(
                    id=doc_key.decode("utf-8") if isinstance(doc_key, bytes) else str(doc_key),
                    score=dist,
                    query=data.get(b"query", b"").decode("utf-8"),
                    text=data.get(b"text", b"").decode("utf-8"),
                    mode=data.get(b"mode", b"").decode("utf-8"),
                    rag_mode=data.get(b"rag_mode", b"").decode("utf-8"),
                    sources_json=data.get(b"sources_json", b"").decode("utf-8"),
                    knowledge_facts_json=data.get(b"knowledge_facts_json", b"").decode("utf-8"),
                    created_at=data.get(b"created_at", b"").decode("utf-8"),
                )

        class MockResult:
            def __init__(self, docs: list[Any]) -> None:
                self.docs = docs

        return MockResult([best_doc] if best_doc is not None else [])


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def hset(self, key: Any, mapping: dict[Any, Any]) -> FakePipeline:
        self.ops.append(("hset", (key, mapping), {}))
        return self

    def expire(self, key: Any, ttl: int) -> FakePipeline:
        self.ops.append(("expire", (key, ttl), {}))
        return self

    def hgetall(self, key: Any) -> FakePipeline:
        self.ops.append(("hgetall", (key,), {}))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op, args, _ in self.ops:
            if op == "hset":
                key, mapping = args
                kb = _to_bytes(key)
                mb = {_to_bytes(k): _to_bytes(v) for k, v in mapping.items()}
                self.redis.store[kb] = mb
                results.append(len(mapping))
            elif op == "expire":
                key, ttl = args
                kb = _to_bytes(key)
                self.redis.ttls[kb] = ttl
                results.append(True)
            elif op == "hgetall":
                key = args[0]
                kb = _to_bytes(key)
                results.append(self.redis.store.get(kb, {}))
        return results


class FakeEmbeddingProvider:
    def __init__(self, vector_map: dict[str, list[float]] | None = None) -> None:
        self.spec = EmbeddingSpec(
            name="bge-m3-test",
            model="BAAI/bge-m3",
            model_commit="5617a9f61b028005a4858fdac845db406aefb181",
        )
        self.vector_map = vector_map or {}
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        results: list[list[float]] = []
        for text in texts:
            if text in self.vector_map:
                results.append(self.vector_map[text])
            else:
                v = np.random.randn(1024).astype(np.float32)
                v /= np.linalg.norm(v)
                results.append(v.tolist())
        return results

    async def aclose(self) -> None:
        pass


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, _statement: object) -> None:
        return None

    async def scalar(self, _statement: object) -> None:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _sample_candidate() -> Candidate:
    return Candidate(
        chunk_id="chunk:test-1",
        source_key="article:2304:123",
        text="스우 최소컷은 6.8 환산 이상 권장",
        metadata={"title": "스우 공략", "url": "https://inven.co.kr/test"},
        fusion_score=0.85,
        rerank_score=0.92,
    )


def _sample_knowledge_fact() -> KnowledgeFact:
    return KnowledgeFact(
        relation_id="rel:test-1",
        subject_entity_id="boss:suu",
        subject_name="스우",
        subject_type="boss",
        predicate="has_difficulty",
        object_entity_id="diff:hard",
        object_name="하드",
        object_type="difficulty",
        source_id="src:official",
        source_title="공식 보스 정보",
        source_url="https://maplestory.nexon.com",
        source_version="v1",
    )


def test_serialization_candidate_and_knowledge_fact() -> None:
    candidate = _sample_candidate()
    fact = _sample_knowledge_fact()

    c_data = serialize_candidate(candidate)
    c_back = deserialize_candidate(c_data)
    assert c_back.chunk_id == candidate.chunk_id
    assert c_back.source_key == candidate.source_key
    assert c_back.text == candidate.text
    assert c_back.fusion_score == candidate.fusion_score
    assert c_back.rerank_score == candidate.rerank_score

    f_data = serialize_knowledge_fact(fact)
    f_back = deserialize_knowledge_fact(f_data)
    assert f_back.relation_id == fact.relation_id
    assert f_back.subject_name == fact.subject_name
    assert f_back.predicate == fact.predicate


@pytest.mark.asyncio
async def test_redis_semantic_cache_store_and_search_hit() -> None:
    fake_redis = FakeRedis(has_redisearch=True)
    cache = RedisSemanticCache(
        "redis://127.0.0.1:56379/0",
        similarity_threshold=0.96,
        ttl_seconds=7 * 86400,
        client=fake_redis,  # type: ignore[arg-type]
    )
    await cache.ensure_index()

    v1 = np.ones(1024, dtype=np.float32)
    v1 /= np.linalg.norm(v1)

    candidate = _sample_candidate()
    fact = _sample_knowledge_fact()

    doc_key = await cache.store(
        query="보스 스우 최소컷 알려줘",
        query_vector=v1.tolist(),
        text="스우 최소컷은 6.8 환산 권장입니다.",
        mode=AnswerMode.GENERATED,
        sources=(candidate,),
        knowledge_facts=(fact,),
    )
    assert doc_key.startswith("maple_chat:cache:")
    assert fake_redis.ttls[doc_key.encode()] == 7 * 86400

    # Perturb vector slightly (cosine similarity ~ 0.99 > 0.96)
    noise = np.zeros(1024, dtype=np.float32)
    noise[0] = 0.05
    v_similar = v1 + noise
    v_similar /= np.linalg.norm(v_similar)
    sim = float(np.dot(v1, v_similar))
    assert sim >= 0.96

    entry = await cache.search(v_similar.tolist())
    assert entry is not None
    assert entry.query == "보스 스우 최소컷 알려줘"
    assert entry.text == "스우 최소컷은 6.8 환산 권장입니다."
    assert entry.mode == AnswerMode.GENERATED
    assert len(entry.sources) == 1
    assert entry.sources[0].chunk_id == candidate.chunk_id
    assert len(entry.knowledge_facts) == 1
    assert entry.knowledge_facts[0].relation_id == fact.relation_id
    assert entry.similarity >= 0.96


@pytest.mark.asyncio
async def test_redis_semantic_cache_search_miss_below_threshold() -> None:
    fake_redis = FakeRedis(has_redisearch=True)
    cache = RedisSemanticCache(
        "redis://127.0.0.1:56379/0",
        similarity_threshold=0.96,
        client=fake_redis,  # type: ignore[arg-type]
    )

    v1 = np.zeros(1024, dtype=np.float32)
    v1[0] = 1.0

    await cache.store(
        query="스우 최소컷",
        query_vector=v1.tolist(),
        text="스우 6.8",
        mode=AnswerMode.GENERATED,
        sources=(),
        knowledge_facts=(),
    )

    # Orthogonal vector (cosine similarity = 0.0 < 0.96)
    v2 = np.zeros(1024, dtype=np.float32)
    v2[1] = 1.0

    entry = await cache.search(v2.tolist())
    assert entry is None


@pytest.mark.asyncio
async def test_redis_semantic_cache_standalone_fallback() -> None:
    fake_redis = FakeRedis(has_redisearch=False)
    cache = RedisSemanticCache(
        "redis://127.0.0.1:56379/0",
        similarity_threshold=0.96,
        client=fake_redis,  # type: ignore[arg-type]
    )
    has_index = await cache.ensure_index()
    assert has_index is False

    v1 = np.ones(1024, dtype=np.float32)
    v1 /= np.linalg.norm(v1)

    await cache.store(
        query="코어 강화 순서",
        query_vector=v1.tolist(),
        text="5차 스킬 우선 강화",
        mode=AnswerMode.GENERATED,
        sources=(),
        knowledge_facts=(),
    )

    entry = await cache.search(v1.tolist())
    assert entry is not None
    assert entry.query == "코어 강화 순서"
    assert entry.text == "5차 스킬 우선 강화"
    assert entry.similarity >= 0.99


@pytest.mark.asyncio
async def test_redis_semantic_cache_invalidation() -> None:
    fake_redis = FakeRedis(has_redisearch=True)
    cache = RedisSemanticCache(
        "redis://127.0.0.1:56379/0",
        client=fake_redis,  # type: ignore[arg-type]
    )

    v1 = np.ones(1024, dtype=np.float32)
    v1 /= np.linalg.norm(v1)

    await cache.store(
        query="q1",
        query_vector=v1.tolist(),
        text="a1",
        mode=AnswerMode.GENERATED,
        sources=(),
        knowledge_facts=(),
    )
    await cache.store(
        query="q2",
        query_vector=v1.tolist(),
        text="a2",
        mode=AnswerMode.GENERATED,
        sources=(),
        knowledge_facts=(),
    )
    assert len(fake_redis.store) == 2

    # Invalidate single query
    deleted = await cache.delete_by_query("q1")
    assert deleted is True
    assert len(fake_redis.store) == 1

    # Invalidate on game patch
    count = await cache.invalidate_on_patch("v1.2.3")
    assert count == 1
    assert len(fake_redis.store) == 0


@pytest.mark.asyncio
async def test_semantic_cache_middleware_miss_then_hit() -> None:
    fake_redis = FakeRedis(has_redisearch=True)
    cache = RedisSemanticCache(
        "redis://127.0.0.1:56379/0",
        client=fake_redis,  # type: ignore[arg-type]
    )

    v1 = np.random.randn(1024).astype(np.float32)
    v1 /= np.linalg.norm(v1)

    embedding = FakeEmbeddingProvider(
        {
            "보스 스우 최소컷 얼마인가요?": v1.tolist(),
            "스우 보스 최소컷 얼마인가요?": v1.tolist(),
        }
    )

    candidate = _sample_candidate()
    fact = _sample_knowledge_fact()

    generated_outcome = AnswerOutcome(
        answer_id="ans-original-123",
        mode=AnswerMode.GENERATED,
        text="스우 최소컷은 6.8 환산 권장입니다.\n\n---\n처리 지표: 입력 100 tok",
        sources=(candidate,),
        knowledge_facts=(fact,),
        metrics=AnswerProcessingMetrics(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_per_second=100.0,
            output_tokens_per_second=50.0,
            model_seconds=1.0,
            total_seconds=1.5,
            model_calls=1,
        ),
    )

    qa_service = MagicMock(spec=QAService)
    qa_service.answer = AsyncMock(return_value=generated_outcome)

    middleware = SemanticCacheMiddleware(
        service=qa_service,
        cache=cache,
        embedding=embedding,
        enabled=True,
        similarity_threshold=0.96,
    )

    session = FakeSession()
    req1 = QuestionRequest(
        guild_id=1,
        channel_id=2,
        request_message_id=101,
        requester_hash="req_user_1",
        query="보스 스우 최소컷 얼마인가요?",
        received_at=datetime.now(UTC),
    )

    # 1st Call: Cache MISS -> calls QAService and stores to cache
    out1 = await middleware.answer(session, req1)  # type: ignore[arg-type]
    assert out1.cached is False
    assert qa_service.answer.call_count == 1
    assert len(fake_redis.store) == 1

    # 2nd Call: Similar question -> Cache HIT -> skips QAService!
    session2 = FakeSession()
    req2 = QuestionRequest(
        guild_id=1,
        channel_id=2,
        request_message_id=102,
        requester_hash="req_user_2",
        query="스우 보스 최소컷 얼마인가요?",
        received_at=datetime.now(UTC),
    )

    out2 = await middleware.answer(session2, req2)  # type: ignore[arg-type]
    assert out2.cached is True
    # QAService was NOT called again (call_count stays 1)
    assert qa_service.answer.call_count == 1
    assert out2.answer_id != out1.answer_id
    assert "시맨틱 캐시 적중" in out2.text
    assert out2.metrics is not None
    assert out2.metrics.cache_hit is True
    assert out2.metrics.model_calls == 0
    assert out2.metrics.model_seconds == 0.0

    # Ensure persistence records were created in session2
    assert any(isinstance(obj, Answer) for obj in session2.added)
    assert any(isinstance(obj, AnswerSource) for obj in session2.added)
    assert any(isinstance(obj, AnswerKnowledgeSource) for obj in session2.added)


@pytest.mark.asyncio
async def test_semantic_cache_middleware_redis_failure_safe_fallback() -> None:
    cache = MagicMock(spec=RedisSemanticCache)
    cache.search = AsyncMock(side_effect=Exception("Redis connection refused"))
    cache.store = AsyncMock(side_effect=Exception("Redis write timeout"))

    qa_service = MagicMock(spec=QAService)
    qa_service.answer = AsyncMock(
        return_value=AnswerOutcome(
            answer_id="fallback-id",
            mode=AnswerMode.GENERATED,
            text="정상 대체 답변",
            sources=(),
            knowledge_facts=(),
        )
    )

    embedding = FakeEmbeddingProvider()
    middleware = SemanticCacheMiddleware(
        service=qa_service,
        cache=cache,
        embedding=embedding,
    )

    session = FakeSession()
    req = QuestionRequest(
        guild_id=1,
        channel_id=2,
        request_message_id=103,
        requester_hash="user",
        query="하드 데미안 스펙",
        received_at=datetime.now(UTC),
    )

    # Should not raise exception; must gracefully fall back to qa_service
    outcome = await middleware.answer(session, req)  # type: ignore[arg-type]
    assert outcome.text == "정상 대체 답변"
    assert qa_service.answer.call_count == 1


def test_processing_metrics_footer_cache_hit() -> None:
    metrics = AnswerProcessingMetrics(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        input_tokens_per_second=None,
        output_tokens_per_second=None,
        model_seconds=0.0,
        total_seconds=0.015,
        model_calls=0,
        embedding_seconds=0.012,
        cache_hit=True,
        cache_similarity=0.985,
    )
    footer = processing_metrics_footer(metrics)
    assert "시맨틱 캐시 적중 (유사도 98.5%)" in footer
    assert "모델 호출 0회 (GPU 절감)" in footer
    assert "0.015초" in footer
    assert "임베딩 0.012초" in footer
