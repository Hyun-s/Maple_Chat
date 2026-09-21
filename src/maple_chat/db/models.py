"""Canonical PostgreSQL schema for durable Maple Chat state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum(enum: type[StrEnum], name: str) -> sa.Enum:
    return sa.Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class SourceStatus(StrEnum):
    DISCOVERED = "discovered"
    FETCHED = "fetched"
    PARSED = "parsed"
    SANITIZED = "sanitized"
    CHUNKED = "chunked"
    INDEXED = "indexed"
    RETRYABLE_FAILED = "retryable_failed"
    TERMINAL_FAILED = "terminal_failed"
    DELETED = "deleted"
    DENIED = "denied"


class JobState(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    DEAD_LETTER = "dead_letter"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceType(StrEnum):
    ARTICLE = "article"
    COMMENT = "comment"
    OCR = "ocr"


class AnswerMode(StrEnum):
    GENERATED = "generated"
    DIRECT = "direct"
    RETRIEVAL_ONLY = "retrieval_only"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class AgentCallState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Board(Base):
    __tablename__ = "boards"

    board_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    crawl_checkpoint: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (sa.UniqueConstraint("board_id", "remote_name"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    board_id: Mapped[int] = mapped_column(
        sa.ForeignKey("boards.board_id", ondelete="CASCADE"), nullable=False
    )
    remote_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        sa.UniqueConstraint("board_id", "remote_article_id"),
        sa.Index(
            "ix_articles_active_board", "board_id", postgresql_where=sa.text("status = 'indexed'")
        ),
        sa.Index(
            "ix_articles_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        sa.Index(
            "ix_articles_body_trgm",
            "sanitized_body",
            postgresql_using="gin",
            postgresql_ops={"sanitized_body": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    board_id: Mapped[int] = mapped_column(
        sa.ForeignKey("boards.board_id", ondelete="RESTRICT"), nullable=False
    )
    remote_article_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("categories.id", ondelete="SET NULL")
    )
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    author_hash: Mapped[str | None] = mapped_column(sa.String(128))
    sanitized_body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    source_modified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    view_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    recommendation_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    comment_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    status: Mapped[SourceStatus] = mapped_column(
        _enum(SourceStatus, "source_status"), nullable=False, default=SourceStatus.SANITIZED
    )
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    comments: Mapped[list[Comment]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        sa.UniqueConstraint("article_id", "remote_comment_id"),
        sa.Index(
            "ix_comments_article_active",
            "article_id",
            postgresql_where=sa.text("status = 'indexed'"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    remote_comment_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    parent_remote_comment_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    author_hash: Mapped[str | None] = mapped_column(sa.String(128))
    sanitized_body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    score: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    status: Mapped[SourceStatus] = mapped_column(
        _enum(SourceStatus, "comment_source_status"),
        nullable=False,
        default=SourceStatus.SANITIZED,
    )
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    article: Mapped[Article] = relationship(back_populates="comments")


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (sa.UniqueConstraint("article_id", "source_url_hash"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_url_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(sa.String(128))
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    fetch_status: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    ocr_text: Mapped[str | None] = mapped_column(sa.Text)
    ocr_confidence: Mapped[float | None] = mapped_column(sa.Float)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        sa.Index(
            "uq_chunks_active_source_ordinal_revision",
            "source_type",
            "source_key",
            "ordinal",
            "chunking_revision",
            unique=True,
            postgresql_where=sa.text("active"),
        ),
        sa.Index("ix_chunks_active", "active", postgresql_where=sa.text("active")),
        sa.Index(
            "ix_chunks_text_trgm",
            "text",
            postgresql_using="gin",
            postgresql_ops={"text": "gin_trgm_ops"},
        ),
    )

    chunk_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    source_type: Mapped[SourceType] = mapped_column(
        _enum(SourceType, "chunk_source_type"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    chunking_revision: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class EmbeddingRevision(Base):
    __tablename__ = "embedding_revisions"
    __table_args__ = (
        sa.Index(
            "uq_embedding_revisions_single_active",
            sa.text("(true)"),
            unique=True,
            postgresql_where=sa.text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(128), unique=True, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_commit: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    normalized: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    distance: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        sa.Index(
            "ix_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sa.text("active"),
        ),
    )

    chunk_id: Mapped[str] = mapped_column(
        sa.ForeignKey("chunks.chunk_id", ondelete="CASCADE"), primary_key=True
    )
    revision_id: Mapped[int] = mapped_column(
        sa.ForeignKey("embedding_revisions.id", ondelete="RESTRICT"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    indexed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        sa.Index(
            "uq_crawl_runs_active_type",
            "run_type",
            unique=True,
            postgresql_where=sa.text("state IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[RunState] = mapped_column(_enum(RunState, "run_state"), nullable=False)
    counters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(sa.String(128))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    __table_args__ = (
        sa.Index(
            "uq_crawl_jobs_active_dedupe",
            "job_type",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text("state IN ('queued', 'leased')"),
        ),
        sa.Index("ix_crawl_jobs_ready", "state", "next_attempt_at", "priority"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    job_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[JobState] = mapped_column(
        _enum(JobState, "job_state"), nullable=False, default=JobState.QUEUED
    )
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="5")
    lease_owner: Mapped[str | None] = mapped_column(sa.String(128))
    leased_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_error_code: Mapped[str | None] = mapped_column(sa.String(128))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    topic: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    aggregate_key: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(sa.String(320), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Tombstone(Base):
    __tablename__ = "tombstones"
    __table_args__ = (sa.UniqueConstraint("source_type", "source_key"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    source_type: Mapped[SourceType] = mapped_column(
        _enum(SourceType, "tombstone_source_type"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    deleted_observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(sa.String(128), nullable=False)


class Answer(Base):
    __tablename__ = "answers"

    answer_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    guild_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    request_message_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    response_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    requester_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    query_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    mode: Mapped[AnswerMode] = mapped_column(_enum(AnswerMode, "answer_mode"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AnswerSource(Base):
    __tablename__ = "answer_sources"
    __table_args__ = (sa.UniqueConstraint("answer_id", "rank"),)

    answer_id: Mapped[str] = mapped_column(
        sa.ForeignKey("answers.answer_id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id: Mapped[str] = mapped_column(
        sa.ForeignKey("chunks.chunk_id", ondelete="RESTRICT"), primary_key=True
    )
    retrieval_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    rerank_score: Mapped[float | None] = mapped_column(sa.Float)
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class AgentRun(Base):
    """Durable plan and checkpoint for one user-scoped Agent execution."""

    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(sa.String(36), primary_key=True)
    guild_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    request_message_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    requester_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    query_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[RunState] = mapped_column(
        _enum(RunState, "agent_run_state"), nullable=False, default=RunState.QUEUED
    )
    plan_json: Mapped[dict[str, Any]] = mapped_column(
        "plan", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(
        "checkpoint", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    tool_call_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_tool_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    resume_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(sa.String(128))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AgentToolCall(Base):
    """Validated arguments and bounded result for a single Agent tool transition."""

    __tablename__ = "agent_tool_calls"
    __table_args__ = (sa.UniqueConstraint("run_id", "step_index"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column("arguments", JSONB, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column("result", JSONB, nullable=False)
    state: Mapped[AgentCallState] = mapped_column(
        _enum(AgentCallState, "agent_call_state"), nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(sa.String(128))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class KnowledgeSource(Base):
    """Versioned provenance for curated, authoritative graph data."""

    __tablename__ = "knowledge_sources"

    source_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    publisher: Mapped[str] = mapped_column(sa.Text, nullable=False)
    version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class KnowledgeEntity(Base):
    """A canonical node in the project knowledge graph."""

    __tablename__ = "knowledge_entities"
    __table_args__ = (
        sa.Index(
            "ix_knowledge_entities_name_trgm",
            "canonical_name",
            postgresql_using="gin",
            postgresql_ops={"canonical_name": "gin_trgm_ops"},
        ),
    )

    entity_id: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    entity_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    canonical_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    source_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class KnowledgeAlias(Base):
    """A data-managed surface form used for generic entity linking."""

    __tablename__ = "knowledge_aliases"
    __table_args__ = (
        sa.Index(
            "ix_knowledge_aliases_normalized_trgm",
            "normalized_alias",
            postgresql_using="gin",
            postgresql_ops={"normalized_alias": "gin_trgm_ops"},
        ),
    )

    normalized_alias: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    entity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_entities.entity_id", ondelete="CASCADE"), primary_key=True
    )
    alias: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alias_type: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())


class KnowledgeRelation(Base):
    """A provenance-bearing directed edge between two canonical entities."""

    __tablename__ = "knowledge_relations"
    __table_args__ = (
        sa.UniqueConstraint("subject_entity_id", "predicate", "object_entity_id"),
        sa.Index("ix_knowledge_relations_subject", "subject_entity_id", "predicate"),
        sa.Index("ix_knowledge_relations_object", "object_entity_id", "predicate"),
    )

    relation_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    subject_entity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_entities.entity_id", ondelete="RESTRICT"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    object_entity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_entities.entity_id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class AnswerKnowledgeSource(Base):
    """Exact graph edges used to produce an answer."""

    __tablename__ = "answer_knowledge_sources"
    __table_args__ = (sa.UniqueConstraint("answer_id", "rank"),)

    answer_id: Mapped[str] = mapped_column(
        sa.ForeignKey("answers.answer_id", ondelete="CASCADE"), primary_key=True
    )
    relation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("knowledge_relations.relation_id", ondelete="RESTRICT"), primary_key=True
    )
    rank: Mapped[int] = mapped_column(sa.Integer, nullable=False)


class GuildSettings(Base):
    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    allowed_channel_ids: Mapped[list[int]] = mapped_column(ARRAY(sa.BigInteger), nullable=False)
    owner_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    allowed_role_ids: Mapped[list[int]] = mapped_column(
        ARRAY(sa.BigInteger), nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class DenylistEntry(Base):
    __tablename__ = "denylist"
    __table_args__ = (
        sa.Index(
            "uq_denylist_active_source",
            "source_key",
            unique=True,
            postgresql_where=sa.text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    normalized_url: Mapped[str | None] = mapped_column(sa.Text)
    source_key: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    actor_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (sa.CheckConstraint("rating BETWEEN 1 AND 5"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    answer_id: Mapped[str] = mapped_column(
        sa.ForeignKey("answers.answer_id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.String(256))
    actor_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    actor_hash: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    target: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    result: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
