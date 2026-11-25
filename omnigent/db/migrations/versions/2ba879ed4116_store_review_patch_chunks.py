"""Store review diffs in byte-bounded chunks.

Revision ID: 2ba879ed4116
Revises: 71ade40e9b21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "2ba879ed4116"
down_revision: str | None = "71ade40e9b21"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Local development may have applied an earlier shape of the parent revision.
    connection = op.get_bind()
    if "patch_text" in {
        column["name"] for column in sa.inspect(connection).get_columns("studio_review_runs")
    }:
        if connection.scalar(
            sa.text("SELECT 1 FROM studio_review_runs WHERE patch_text IS NOT NULL LIMIT 1")
        ):
            raise RuntimeError("Migrate captured review run diffs before replacing patch_text")
        with op.batch_alter_table("studio_review_runs") as batch:
            batch.drop_column("patch_text")
    op.create_table(
        "studio_review_patch_chunks",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("position", sa.SmallInteger(), primary_key=True),
        sa.Column("payload", sa.LargeBinary(12288), nullable=False),
        sa.CheckConstraint("position >= 0 AND position < 4", name="ck_review_patch_position"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM studio_review_patch_chunks LIMIT 1")):
        raise RuntimeError("Cannot downgrade while captured review run diffs exist")
    op.drop_table("studio_review_patch_chunks")
