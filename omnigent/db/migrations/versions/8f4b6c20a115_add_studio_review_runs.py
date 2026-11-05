"""Add revision-scoped project review runs.

Revision ID: 8f4b6c20a115
Revises: 5f9ca09e07cd
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "8f4b6c20a115"
down_revision: str | None = "5f9ca09e07cd"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    digest = sa.LargeBinary(32).with_variant(BINARY(32), "mysql")
    op.create_table(
        "studio_review_runs",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("owner_scope", digest, nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("pr_key", digest, nullable=False),
        sa.Column("request_key", digest, nullable=False),
        sa.Column("request_hash", digest, nullable=False),
        sa.Column("target_json", CompressedText(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "owner_scope", "request_key", name="uq_review_run_key"
        ),
    )
    op.create_index(
        "ix_review_run_project_pr",
        "studio_review_runs",
        ["workspace_id", "owner_scope", "project_id", "pr_key", "created_at", "id"],
    )
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.add_column(sa.Column("run_id", Uuid16(), nullable=True))
        batch.create_index("ix_review_pass_run", ["workspace_id", "run_id", "created_at", "id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM studio_review_runs LIMIT 1")):
        raise RuntimeError("Cannot downgrade while review runs exist")
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.drop_index("ix_review_pass_run")
        batch.drop_column("run_id")
    op.drop_table("studio_review_runs")
