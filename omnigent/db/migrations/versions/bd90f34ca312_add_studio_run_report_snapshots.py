"""Preserve completed reports and blocked launch reasons for review runs.

Revision ID: bd90f34ca312
Revises: 8f4b6c20a115
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText

revision: str = "bd90f34ca312"
down_revision: str | None = "8f4b6c20a115"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.add_column(sa.Column("launch_error", sa.String(512), nullable=True))
        batch.add_column(sa.Column("report_json", CompressedText(), nullable=True))

    # Early local builds of the run migration supplied a database default.
    with op.batch_alter_table("studio_review_runs") as batch:
        batch.alter_column("workspace_id", existing_type=sa.BigInteger(), server_default=None)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_passes WHERE report_json IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while saved review run reports exist")
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.drop_column("report_json")
        batch.drop_column("launch_error")
