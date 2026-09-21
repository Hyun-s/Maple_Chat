"""g008 preserve replaced chunk history

Revision ID: g008_chunk_history
Revises: g007_direct_answers
Create Date: 2026-08-22 20:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g008_chunk_history"
down_revision: str | Sequence[str] | None = "g007_direct_answers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CONSTRAINT = "chunks_source_type_source_key_ordinal_chunking_revision_key"
_ACTIVE_INDEX = "uq_chunks_active_source_ordinal_revision"


def upgrade() -> None:
    op.drop_constraint(_OLD_CONSTRAINT, "chunks", type_="unique")
    op.create_index(
        _ACTIVE_INDEX,
        "chunks",
        ["source_type", "source_key", "ordinal", "chunking_revision"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(_ACTIVE_INDEX, table_name="chunks", postgresql_where=sa.text("active"))

    # A downgrade can only retain one version per natural key. Point historical
    # answer citations at the preferred (active/newest) version before pruning.
    op.execute(
        """
        WITH mapping AS (
            SELECT
                chunk_id,
                first_value(chunk_id) OVER (
                    PARTITION BY source_type, source_key, ordinal, chunking_revision
                    ORDER BY active DESC, created_at DESC, chunk_id DESC
                ) AS keep_chunk_id
            FROM chunks
        ), ranked_sources AS (
            SELECT
                answer_sources.ctid AS source_row,
                row_number() OVER (
                    PARTITION BY answer_sources.answer_id, mapping.keep_chunk_id
                    ORDER BY answer_sources.rank
                ) AS source_rank
            FROM answer_sources
            JOIN mapping ON mapping.chunk_id = answer_sources.chunk_id
        )
        DELETE FROM answer_sources
        USING ranked_sources
        WHERE answer_sources.ctid = ranked_sources.source_row
          AND ranked_sources.source_rank > 1
        """
    )
    op.execute(
        """
        WITH mapping AS (
            SELECT
                chunk_id,
                first_value(chunk_id) OVER (
                    PARTITION BY source_type, source_key, ordinal, chunking_revision
                    ORDER BY active DESC, created_at DESC, chunk_id DESC
                ) AS keep_chunk_id
            FROM chunks
        )
        UPDATE answer_sources
        SET chunk_id = mapping.keep_chunk_id
        FROM mapping
        WHERE answer_sources.chunk_id = mapping.chunk_id
          AND mapping.chunk_id <> mapping.keep_chunk_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                chunk_id,
                row_number() OVER (
                    PARTITION BY source_type, source_key, ordinal, chunking_revision
                    ORDER BY active DESC, created_at DESC, chunk_id DESC
                ) AS version_rank
            FROM chunks
        )
        DELETE FROM chunks
        USING ranked
        WHERE chunks.chunk_id = ranked.chunk_id
          AND ranked.version_rank > 1
        """
    )
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        "chunks",
        ["source_type", "source_key", "ordinal", "chunking_revision"],
    )
