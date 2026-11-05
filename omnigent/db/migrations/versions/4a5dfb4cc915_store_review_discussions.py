"""Store private review grants and revision-bound discussions.

Revision ID: 4a5dfb4cc915
Revises: 5a7d60f93b12

Apply before releasing review discussions. Older binaries ignore these tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32, CompressedText, Uuid16

revision: str = "4a5dfb4cc915"
down_revision: str | None = "5a7d60f93b12"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_review_collaborators",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("user_key", _CKSUM32, primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("user_generation", Uuid16(), nullable=True),
        sa.Column("owner_generation", Uuid16(), nullable=True),
    )
    op.create_table(
        "studio_review_comments",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("author_user_id", sa.String(128), nullable=True),
        sa.Column("author_generation", Uuid16(), nullable=True),
        sa.Column("anchor_json", CompressedText(), nullable=False),
        sa.Column("body", CompressedText(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    for table in ("studio_review_comments", "studio_review_collaborators"):
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError("Cannot downgrade while review discussions or grants exist")
    op.drop_table("studio_review_comments")
    op.drop_table("studio_review_collaborators")
