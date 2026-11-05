"""Bind captured review runs to the owner's account incarnation.

Revision ID: 18b794326fcb
Revises: 4a5dfb4cc915

Apply before releasing discussion access. Older binaries ignore the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "18b794326fcb"
down_revision: str | None = "4a5dfb4cc915"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("studio_review_runs", sa.Column("owner_generation", Uuid16(), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_runs WHERE owner_generation IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot discard captured review owner identities")
    op.drop_column("studio_review_runs", "owner_generation")
