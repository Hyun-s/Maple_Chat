"""Concrete database, local-model, and Discord runtime wiring."""

from __future__ import annotations

import logging
import time
from dataclasses import replace

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maple_chat.agent.local_tools import build_local_tool_registry
from maple_chat.agent.service import (
    AgentNotConfiguredService,
    DurableAgentService,
    NexonAgentService,
)
from maple_chat.agent.tools import build_nexon_tool_registry
from maple_chat.config import Settings
from maple_chat.db.models import Answer, GuildSettings
from maple_chat.db.session import create_engine, create_session_factory
from maple_chat.discord.routing import MessageEnvelope
from maple_chat.discord.runtime import MapleDiscordClient
from maple_chat.indexing.embedding import (
    EmbeddingProvider,
    EmbeddingSpec,
    RemoteEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
)
from maple_chat.knowledge.catalog import normalize_alias
from maple_chat.knowledge.service import (
    DatabaseKnowledgeRetriever,
    KnowledgeFact,
    sync_builtin_knowledge,
)
from maple_chat.llm.client import LocalLLMClient
from maple_chat.nexon.client import NexonOpenAPIClient
from maple_chat.ops.logging import configure_json_logging
from maple_chat.qa.service import (
    AnswerOutcome,
    QAService,
    QuestionRequest,
    SourceView,
    load_answer_sources,
)
from maple_chat.retrieval.hybrid import (
    RemoteReranker,
    Reranker,
    RerankerSpec,
    RetrievalResult,
    SentenceTransformerReranker,
    hybrid_retrieve,
)
from maple_chat.retrieval.modes import RAGQueryMode
from maple_chat.time import utc_now

logger = logging.getLogger(__name__)


def embedding_spec(settings: Settings) -> EmbeddingSpec:
    return EmbeddingSpec(
        name=f"bge-m3-{settings.embedding_model_commit[:12]}",
        model=settings.embedding_model,
        model_commit=settings.embedding_model_commit,
    )


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    spec = embedding_spec(settings)
    if settings.embedding_provider == "remote":
        return RemoteEmbeddingProvider(
            spec,
            base_url=settings.embedding_base_url,
            served_model=settings.embedding_remote_model,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return SentenceTransformerEmbeddingProvider(spec, device=settings.embedding_device)


def reranker_spec(settings: Settings) -> RerankerSpec:
    return RerankerSpec(settings.reranker_model, settings.reranker_model_commit)


def build_reranker(settings: Settings) -> Reranker:
    spec = reranker_spec(settings)
    if settings.reranker_provider == "remote":
        return RemoteReranker(
            spec,
            base_url=settings.reranker_base_url,
            served_model=settings.reranker_remote_model,
            timeout_seconds=settings.reranker_timeout_seconds,
        )
    logger.warning(
        "reranker_provider is not remote: running the BGE cross-encoder in-process on this host",
    )
    return SentenceTransformerReranker(spec)


class DatabaseHybridRetriever:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        embedding: EmbeddingProvider,
        reranker: Reranker,
        knowledge_retriever: DatabaseKnowledgeRetriever | None = None,
    ) -> None:
        self.factory = factory
        self.embedding = embedding
        self.reranker = reranker
        self.knowledge_retriever = knowledge_retriever

    async def retrieve(self, query: str, *, use_knowledge_anchors: bool = True) -> RetrievalResult:
        facts: tuple[KnowledgeFact, ...]
        embedding_started_at = time.perf_counter()
        if self.knowledge_retriever is None or not use_knowledge_anchors:
            vectors = await self.embedding.embed([query])
            facts = ()
        else:
            facts = await self.knowledge_retriever.retrieve(query)
            vectors = await self.embedding.embed([query])
        if len(vectors) != 1:
            raise RuntimeError("query embedding provider returned an invalid batch")
        embedding_seconds = time.perf_counter() - embedding_started_at
        normalized_query = normalize_alias(query)
        anchor_terms = tuple(
            dict.fromkeys(
                name
                for fact in facts
                for name in (fact.subject_name, fact.object_name)
                if normalize_alias(name) in normalized_query
            )
        )
        required_scope_groups = tuple(
            names
            for entity_type in ("job", "boss")
            if (
                names := tuple(
                    dict.fromkeys(
                        name
                        for fact in facts
                        for name, current_type in (
                            (fact.subject_name, fact.subject_type),
                            (fact.object_name, fact.object_type),
                        )
                        if current_type == entity_type and normalize_alias(name) in normalized_query
                    )
                )
            )
        )
        result = await hybrid_retrieve(
            self.factory,
            query=query,
            query_vector=vectors[0],
            reranker=self.reranker,
            now=utc_now(),
            anchor_terms=anchor_terms,
            required_scope_groups=required_scope_groups,
        )
        return replace(result, embedding_seconds=embedding_seconds)


class DatabaseAnswerHandler:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        service: QAService,
        agent_service: DurableAgentService | AgentNotConfiguredService,
    ) -> None:
        self.factory = factory
        self.service = service
        self.agent_service = agent_service

    async def handle(
        self,
        message: MessageEnvelope,
        question: str,
        requester_hash: str,
        rag_enabled: bool,
        rag_mode: RAGQueryMode,
        agent_enabled: bool,
        agent_resume_id: str | None,
    ) -> AnswerOutcome:
        if message.guild_id is None:
            raise ValueError("Discord direct messages are outside the configured guild")
        request = QuestionRequest(
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            request_message_id=message.message_id,
            requester_hash=requester_hash,
            query=question,
            received_at=utc_now(),
            rag_enabled=rag_enabled,
            rag_mode=rag_mode,
        )
        if agent_enabled:
            outcome = await self.agent_service.answer(request, resume_run_id=agent_resume_id)
        else:
            async with self.factory() as session, session.begin():
                outcome = await self.service.answer(session, request)
        metrics = outcome.metrics
        logger.info(
            "discord_answer_completed",
            extra={
                "context": {
                    "answer_id": outcome.answer_id,
                    "mode": outcome.mode.value,
                    "rag_enabled": rag_enabled,
                    "rag_query_mode": rag_mode.value,
                    "agent_enabled": agent_enabled,
                    "agent_resumed": agent_resume_id is not None,
                    "duplicate": outcome.duplicate,
                    "input_tokens": metrics.input_tokens if metrics is not None else 0,
                    "output_tokens": metrics.output_tokens if metrics is not None else 0,
                    "total_tokens": metrics.total_tokens if metrics is not None else 0,
                    "input_tokens_per_second": (
                        metrics.input_tokens_per_second if metrics is not None else None
                    ),
                    "output_tokens_per_second": (
                        metrics.output_tokens_per_second if metrics is not None else None
                    ),
                    "model_seconds": metrics.model_seconds if metrics is not None else 0.0,
                    "total_seconds": metrics.total_seconds if metrics is not None else 0.0,
                    "model_calls": metrics.model_calls if metrics is not None else 0,
                    "embedding_seconds": (
                        metrics.embedding_seconds if metrics is not None else None
                    ),
                    "rerank_seconds": (metrics.rerank_seconds if metrics is not None else None),
                    "retrieval_seconds": (
                        metrics.retrieval_seconds if metrics is not None else None
                    ),
                }
            },
        )
        return outcome


class DatabaseSourceLoader:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def load(self, answer_id: str, requester_hash: str) -> tuple[SourceView, ...]:
        async with self.factory() as session:
            return await load_answer_sources(
                session,
                answer_id=answer_id,
                requester_hash=requester_hash,
                now=utc_now(),
            )

    async def active_answer_ids(self) -> tuple[str, ...]:
        async with self.factory() as session:
            answer_ids = await session.scalars(
                sa.select(Answer.answer_id)
                .where(Answer.expires_at > utc_now())
                .order_by(Answer.created_at)
            )
            return tuple(answer_ids)


async def configure_or_load_channels(
    factory: async_sessionmaker[AsyncSession],
    *,
    guild_id: int,
    owner_id: int,
    configured_channel_ids: tuple[int, ...],
) -> frozenset[int]:
    async with factory() as session, session.begin():
        if configured_channel_ids:
            await session.execute(
                insert(GuildSettings)
                .values(
                    guild_id=guild_id,
                    allowed_channel_ids=list(configured_channel_ids),
                    owner_id=owner_id,
                    allowed_role_ids=[],
                )
                .on_conflict_do_update(
                    index_elements=[GuildSettings.guild_id],
                    set_={
                        "allowed_channel_ids": list(configured_channel_ids),
                        "owner_id": owner_id,
                        "updated_at": sa.func.now(),
                    },
                )
            )
        row = await session.get(GuildSettings, guild_id)
        if row is None or not row.allowed_channel_ids:
            raise RuntimeError(
                "DISCORD_CHANNEL_IDS is required on first start; use comma-separated channel IDs"
            )
        if row.owner_id != owner_id:
            raise RuntimeError("stored Discord owner does not match DISCORD_OWNER_ID")
        return frozenset(int(channel_id) for channel_id in row.allowed_channel_ids)


async def check_database(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(sa.select(sa.literal(1)))


async def run_discord_bot(settings: Settings) -> None:
    configure_json_logging(settings.log_level)
    if (
        settings.discord_token is None
        or not settings.effective_discord_guild_ids
        or settings.discord_owner_id is None
        or settings.pii_hash_salt is None
    ):
        raise RuntimeError("Discord runtime configuration is incomplete")

    engine = create_engine(settings)
    factory = create_session_factory(engine)
    llm = LocalLLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    nexon_client: NexonOpenAPIClient | None = None
    embedding: EmbeddingProvider | None = None
    reranker: Reranker | None = None
    try:
        await check_database(factory)
        async with factory() as session, session.begin():
            await sync_builtin_knowledge(session, synced_at=utc_now())
        guild_ids = frozenset(int(value) for value in settings.effective_discord_guild_ids)
        configured_channel_ids = tuple(int(value) for value in settings.discord_channel_ids)
        channel_ids: set[int] = set()
        for guild_id in guild_ids:
            channel_ids.update(
                await configure_or_load_channels(
                    factory,
                    guild_id=guild_id,
                    owner_id=int(settings.discord_owner_id),
                    configured_channel_ids=configured_channel_ids,
                )
            )
        embedding = build_embedding_provider(settings)
        reranker = build_reranker(settings)
        knowledge_retriever = DatabaseKnowledgeRetriever(factory)
        retriever = DatabaseHybridRetriever(
            factory, embedding, reranker, knowledge_retriever=knowledge_retriever
        )
        if settings.nexon_api_key is None:
            agent_service: DurableAgentService | AgentNotConfiguredService = (
                AgentNotConfiguredService(factory)
            )
        else:
            nexon_client = NexonOpenAPIClient(api_key=settings.nexon_api_key.get_secret_value())
            registry = build_nexon_tool_registry(nexon_client).merged_with(
                build_local_tool_registry(retriever, knowledge_retriever)
            )
            agent_service = DurableAgentService(
                factory,
                NexonAgentService(
                    llm,
                    registry,
                    max_tool_calls=settings.agent_max_tool_calls,
                ),
            )
        handler = DatabaseAnswerHandler(
            factory,
            QAService(retriever, llm, knowledge_retriever=knowledge_retriever),
            agent_service,
        )
        source_loader = DatabaseSourceLoader(factory)
        client = MapleDiscordClient(
            guild_ids=guild_ids,
            channel_ids=frozenset(channel_ids),
            pii_salt=settings.pii_hash_salt.get_secret_value(),
            handler=handler,
            source_loader=source_loader,
        )
        try:
            await client.start(settings.discord_token.get_secret_value(), reconnect=True)
        finally:
            if not client.is_closed():
                await client.close()
    finally:
        if nexon_client is not None:
            await nexon_client.aclose()
        if embedding is not None:
            await embedding.aclose()
        if isinstance(reranker, RemoteReranker):
            await reranker.aclose()
        await llm.aclose()
        await engine.dispose()
