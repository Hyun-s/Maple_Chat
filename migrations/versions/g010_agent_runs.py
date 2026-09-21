"""g010 durable Agent runs and tool checkpoints

Revision ID: g010_agent_runs
Revises: g009_knowledge_graph
Create Date: 2026-08-31 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "g010_agent_runs"
down_revision: str | Sequence[str] | None = "g009_knowledge_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("request_message_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("requester_hash", sa.String(128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(9), nullable=False),
        sa.Column(
            "plan",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued','running','partial','succeeded','failed','cancelled')",
            name="agent_run_state",
        ),
        sa.CheckConstraint("max_tool_calls BETWEEN 1 AND 8", name="agent_max_tool_calls"),
        sa.CheckConstraint("tool_call_count >= 0", name="agent_tool_call_count_nonnegative"),
    )
    op.create_index("ix_agent_runs_requester_state", "agent_runs", ["requester_hash", "state"])
    op.create_index("ix_agent_runs_expires_at", "agent_runs", ["expires_at"])
    op.create_table(
        "agent_tool_calls",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(9), nullable=False),
        sa.Column("error_code", sa.String(128)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("state IN ('succeeded','failed')", name="agent_call_state"),
        sa.UniqueConstraint("run_id", "step_index"),
    )
    op.create_index("ix_agent_tool_calls_run", "agent_tool_calls", ["run_id", "step_index"])


def downgrade() -> None:
    op.drop_index("ix_agent_tool_calls_run", table_name="agent_tool_calls")
    op.drop_table("agent_tool_calls")
    op.drop_index("ix_agent_runs_expires_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_requester_state", table_name="agent_runs")
    op.drop_table("agent_runs")
