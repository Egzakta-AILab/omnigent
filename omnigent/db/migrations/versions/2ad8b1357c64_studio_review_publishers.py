"""Store delegated inline publication grants and confirmation attribution.

Revision ID: 2ad8b1357c64
Revises: b51ac8e4d231
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "2ad8b1357c64"
down_revision: str | None = "b51ac8e4d231"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "studio_review_collaborators",
        sa.Column("can_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("studio_inline_drafts", sa.Column("publication_confirmed_by", sa.String(256)))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        bind.exec_driver_sql("BEGIN")
    if bind.scalar(sa.text("SELECT 1 FROM studio_review_collaborators WHERE can_publish LIMIT 1")):
        raise RuntimeError("Cannot downgrade while review publishers are assigned")
    if bind.scalar(
        sa.text(
            "SELECT 1 FROM studio_inline_drafts WHERE publication_confirmed_by IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot downgrade while publication confirmations exist")
    with op.batch_alter_table("studio_inline_drafts") as batch:
        batch.drop_column("publication_confirmed_by")
    with op.batch_alter_table("studio_review_collaborators") as batch:
        batch.drop_column("can_publish")
