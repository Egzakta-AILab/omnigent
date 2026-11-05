"""Store durable review questions and explicit answer sharing.

Revision ID: 3792ec30b1de
Revises: 18b794326fcb

Apply before releasing review questions. Older binaries ignore the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32, CompressedText, Uuid16

revision: str = "3792ec30b1de"
down_revision: str | None = "18b794326fcb"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_review_questions",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("actor_scope", _CKSUM32, nullable=False),
        sa.Column("actor_user_id", sa.String(128), nullable=True),
        sa.Column("actor_generation", Uuid16(), nullable=True),
        sa.Column("intent_hash", _CKSUM32, nullable=False),
        sa.Column("input_json", CompressedText(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("answer_json", CompressedText(), nullable=True),
        sa.Column("published_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM studio_review_questions LIMIT 1")):
        raise RuntimeError("Cannot downgrade while saved review questions exist")
    op.drop_table("studio_review_questions")
