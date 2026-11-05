"""Store orchestrated review rosters and approved plans.

Revision ID: 0aa7e513ddb0
Revises: 2ba879ed4116
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import CompressedText

revision: str = "0aa7e513ddb0"
down_revision: str | None = "2ba879ed4116"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column("studio_review_runs", sa.Column("plan_json", CompressedText(), nullable=True))
    op.add_column("studio_review_passes", sa.Column("run_role", sa.String(16), nullable=True))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_review_passes WHERE run_role IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while orchestrated review runs exist")
    op.drop_column("studio_review_passes", "run_role")
    op.drop_column("studio_review_runs", "plan_json")
