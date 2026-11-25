"""Retain the exact diff supplied to each newly captured review run.

Revision ID: 71ade40e9b21
Revises: bd90f34ca312
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "71ade40e9b21"
down_revision: str | None = "bd90f34ca312"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("studio_review_runs") as batch:
        batch.add_column(sa.Column("patch_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_runs WHERE patch_sha256 IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while captured review run diffs exist")
    with op.batch_alter_table("studio_review_runs") as batch:
        batch.drop_column("patch_sha256")
