"""Store bounded reproduction receipts for saved findings.

Revision ID: 5a7d60f93b12
Revises: 0aa7e513ddb0

Apply before releasing receipt attachments. Older applications ignore the table
and can run with the migration still applied.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32, Uuid16

revision: str = "5a7d60f93b12"
down_revision: str | None = "0aa7e513ddb0"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_review_receipt_chunks",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("finding_key", _CKSUM32, primary_key=True),
        sa.Column("receipt_digest", _CKSUM32, primary_key=True),
        sa.Column("position", sa.SmallInteger(), primary_key=True),
        sa.Column("attached_at", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.LargeBinary(12288), nullable=False),
        sa.CheckConstraint("position >= 0 AND position < 16", name="ck_review_receipt_position"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM studio_review_receipt_chunks LIMIT 1")):
        raise RuntimeError("Cannot downgrade while review reproduction receipts exist")
    op.drop_table("studio_review_receipt_chunks")
