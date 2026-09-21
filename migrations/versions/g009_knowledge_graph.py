"""g009 add provenance-bearing knowledge graph

Revision ID: g009_knowledge_graph
Revises: g008_chunk_history
Create Date: 2026-08-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g009_knowledge_graph"
down_revision: str | Sequence[str] | None = "g008_chunk_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_sources",
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "knowledge_entities",
        sa.Column("entity_id", sa.String(128), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_knowledge_entities_name_trgm",
        "knowledge_entities",
        ["canonical_name"],
        postgresql_using="gin",
        postgresql_ops={"canonical_name": "gin_trgm_ops"},
    )
    op.create_table(
        "knowledge_aliases",
        sa.Column("normalized_alias", sa.Text(), primary_key=True),
        sa.Column(
            "entity_id",
            sa.String(128),
            sa.ForeignKey("knowledge_entities.entity_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("alias_type", sa.String(32), nullable=False),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(
        "ix_knowledge_aliases_normalized_trgm",
        "knowledge_aliases",
        ["normalized_alias"],
        postgresql_using="gin",
        postgresql_ops={"normalized_alias": "gin_trgm_ops"},
    )
    op.create_table(
        "knowledge_relations",
        sa.Column("relation_id", sa.String(64), primary_key=True),
        sa.Column(
            "subject_entity_id",
            sa.String(128),
            sa.ForeignKey("knowledge_entities.entity_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("predicate", sa.String(64), nullable=False),
        sa.Column(
            "object_entity_id",
            sa.String(128),
            sa.ForeignKey("knowledge_entities.entity_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("knowledge_sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("subject_entity_id", "predicate", "object_entity_id"),
    )
    op.create_index(
        "ix_knowledge_relations_subject",
        "knowledge_relations",
        ["subject_entity_id", "predicate"],
    )
    op.create_index(
        "ix_knowledge_relations_object",
        "knowledge_relations",
        ["object_entity_id", "predicate"],
    )
    op.create_table(
        "answer_knowledge_sources",
        sa.Column(
            "answer_id",
            sa.String(36),
            sa.ForeignKey("answers.answer_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "relation_id",
            sa.String(64),
            sa.ForeignKey("knowledge_relations.relation_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.UniqueConstraint("answer_id", "rank"),
    )


def downgrade() -> None:
    op.drop_table("answer_knowledge_sources")
    op.drop_index("ix_knowledge_relations_object", table_name="knowledge_relations")
    op.drop_index("ix_knowledge_relations_subject", table_name="knowledge_relations")
    op.drop_table("knowledge_relations")
    op.drop_index("ix_knowledge_aliases_normalized_trgm", table_name="knowledge_aliases")
    op.drop_table("knowledge_aliases")
    op.drop_index("ix_knowledge_entities_name_trgm", table_name="knowledge_entities")
    op.drop_table("knowledge_entities")
    op.drop_table("knowledge_sources")
