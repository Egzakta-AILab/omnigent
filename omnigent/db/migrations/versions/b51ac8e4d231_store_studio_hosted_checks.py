"""Store sandbox check dispatches on their approved proposal.

Revision ID: b51ac8e4d231
Revises: ba9152e8d96f
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import CompressedText

revision: str = "b51ac8e4d231"
down_revision: str | None = "ba9152e8d96f"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "studio_review_questions", sa.Column("check_job_json", CompressedText(), nullable=True)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        bind.exec_driver_sql("BEGIN")
    if bind.scalar(
        sa.text("SELECT 1 FROM studio_review_questions WHERE check_job_json IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while sandbox check dispatches exist")
    op.drop_column("studio_review_questions", "check_job_json")
