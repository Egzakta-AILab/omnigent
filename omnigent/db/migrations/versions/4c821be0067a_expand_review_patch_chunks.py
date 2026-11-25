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


def _replace_position_limit(limit: int) -> None:
    name = "ck_review_patch_position"
    condition = f"position >= 0 AND position < {limit}"
    if op.get_bind().dialect.name == "cockroachdb":
        # CockroachDB reserves a dropped constraint name until commit. Rename it
        # first so the replacement can take the canonical name atomically.
        op.execute(
            "ALTER TABLE studio_review_patch_chunks RENAME CONSTRAINT "
            "ck_review_patch_position TO ck_review_patch_position_previous"
        )
        with op.batch_alter_table("studio_review_patch_chunks") as batch:
            batch.create_check_constraint(name, condition)
            batch.drop_constraint("ck_review_patch_position_previous", type_="check")
    else:
        with op.batch_alter_table("studio_review_patch_chunks") as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, condition)


def upgrade() -> None:
    _replace_position_limit(64)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_patch_chunks WHERE position >= 4 LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while larger captured review run diffs exist")
    _replace_position_limit(4)
