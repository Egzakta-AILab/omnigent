"""Add project-origin reviews alongside existing session-origin reviews.

Revision ID: 5f9ca09e07cd
Revises: e6a2b4c8d013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "5f9ca09e07cd"
down_revision: str | None = "e6a2b4c8d013"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.add_column(sa.Column("source_project_id", Uuid16(), nullable=True))
        batch.alter_column("source_session_id", existing_type=Uuid16(), nullable=True)
        batch.create_check_constraint(
            "ck_studio_review_origin",
            "(source_session_id IS NULL) <> (source_project_id IS NULL)",
        )
        batch.create_index(
            "ix_studio_review_pass_owner_project",
            ["workspace_id", "owner_scope", "source_project_id", "created_at", "id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT 1 FROM studio_review_passes WHERE source_project_id IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while project-origin review records exist")
    with op.batch_alter_table("studio_review_passes") as batch:
        batch.drop_index("ix_studio_review_pass_owner_project")
        batch.drop_constraint("ck_studio_review_origin", type_="check")
        batch.alter_column("source_session_id", existing_type=Uuid16(), nullable=False)
        batch.drop_column("source_project_id")
