"""g007 direct answers

Revision ID: g007_direct_answers
Revises: g006_run_overlap
Create Date: 2026-08-22 19:35:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "g007_direct_answers"
down_revision: str | Sequence[str] | None = "g006_run_overlap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGINAL_MODES = "'generated', 'retrieval_only', 'insufficient_evidence'"
_DIRECT_MODES = "'generated', 'direct', 'retrieval_only', 'insufficient_evidence'"


def upgrade() -> None:
    op.drop_constraint("answer_mode", "answers", type_="check")
    op.create_check_constraint("answer_mode", "answers", f"mode IN ({_DIRECT_MODES})")


def downgrade() -> None:
    op.execute("UPDATE answers SET mode = 'generated' WHERE mode = 'direct'")
    op.drop_constraint("answer_mode", "answers", type_="check")
    op.create_check_constraint("answer_mode", "answers", f"mode IN ({_ORIGINAL_MODES})")
