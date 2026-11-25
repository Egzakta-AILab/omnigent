"""Track single-claim delivery of inline review drafts.

Revision ID: 7a2594cfe931
Revises: 4bf0aa1e9173
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import _CKSUM32

revision: str = "7a2594cfe931"
down_revision: str | None = "4bf0aa1e9173"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "studio_inline_drafts",
        sa.Column("publication_state", sa.String(16), nullable=False, server_default="draft"),
    )
    op.add_column("studio_inline_drafts", sa.Column("publication_digest", _CKSUM32, nullable=True))
    op.add_column(
        "studio_inline_drafts", sa.Column("provider_url", sa.String(2048), nullable=True)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        # A later revision can refuse rollback after these columns are dropped.
        bind.exec_driver_sql("BEGIN")
    if bind.scalar(
        sa.text("SELECT 1 FROM studio_inline_drafts WHERE publication_state <> 'draft' LIMIT 1")
    ):
        raise RuntimeError("Cannot downgrade while inline publication records exist")
    with op.batch_alter_table("studio_inline_drafts") as batch:
        batch.drop_column("provider_url")
        batch.drop_column("publication_digest")
        batch.drop_column("publication_state")
