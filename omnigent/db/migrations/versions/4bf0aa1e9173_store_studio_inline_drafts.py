"""Store private inline drafts for captured review revisions.

Revision ID: 4bf0aa1e9173
Revises: 3792ec30b1de
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32, CompressedText, Uuid16

revision: str = "4bf0aa1e9173"
down_revision: str | None = "3792ec30b1de"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_inline_drafts",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("actor_scope", _CKSUM32, nullable=False),
        sa.Column("actor_generation", Uuid16(), nullable=True),
        sa.Column("intent_hash", _CKSUM32, nullable=False),
        sa.Column("thread_json", CompressedText(), nullable=False),
        sa.Column("anchor_json", CompressedText(), nullable=False),
        sa.Column("body", CompressedText(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("discarded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_inline_drafts WHERE NOT discarded LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while saved inline drafts exist")
    op.drop_table("studio_inline_drafts")
