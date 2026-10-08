"""g011 chunk tsvector GIN index for lexical pre-filtering

Revision ID: g011_chunk_tsv_lexical
Revises: g010_agent_runs
Create Date: 2026-10-08 16:30:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "g011_chunk_tsv_lexical"
down_revision: str | Sequence[str] | None = "g010_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_tsv "
        "ON chunks USING gin (to_tsvector('simple', text))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_tsv")
