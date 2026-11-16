"""Allow larger saved PR diffs independently of review prompts.

Revision ID: 4c821be0067a
Revises: cee8b56c7a42
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "4c821be0067a"
down_revision: str | None = "cee8b56c7a42"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("studio_review_patch_chunks") as batch:
        batch.drop_constraint("ck_review_patch_position", type_="check")
        batch.create_check_constraint(
            "ck_review_patch_position", "position >= 0 AND position < 64"
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_patch_chunks WHERE position >= 4 LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while larger captured review run diffs exist")
    with op.batch_alter_table("studio_review_patch_chunks") as batch:
        batch.drop_constraint("ck_review_patch_position", type_="check")
        batch.create_check_constraint("ck_review_patch_position", "position >= 0 AND position < 4")
