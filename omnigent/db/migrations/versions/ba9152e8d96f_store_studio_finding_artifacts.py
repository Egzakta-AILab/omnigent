"""Store immutable metadata for finding evidence.

Revision ID: ba9152e8d96f
Revises: 7a2594cfe931
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32, CompressedText, Uuid16

revision: str = "ba9152e8d96f"
down_revision: str | None = "7a2594cfe931"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_finding_artifacts",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", Uuid16(), primary_key=True),
        sa.Column("finding_key", _CKSUM32, primary_key=True),
        sa.Column("digest", _CKSUM32, primary_key=True),
        sa.Column("attached_at", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", CompressedText(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        bind.exec_driver_sql("BEGIN")
    if bind.scalar(sa.text("SELECT 1 FROM studio_finding_artifacts LIMIT 1")):
        raise RuntimeError("Cannot downgrade while finding artifacts exist")
    op.drop_table("studio_finding_artifacts")
