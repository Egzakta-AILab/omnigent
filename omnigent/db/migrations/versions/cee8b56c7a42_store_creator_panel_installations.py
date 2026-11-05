"""Store immutable Creator packages and per-user project selections.

Revision ID: cee8b56c7a42
Revises: 2ad8b1357c64
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

from omnigent.db.db_models import Uuid16

revision: str = "cee8b56c7a42"
down_revision: str | None = "2ad8b1357c64"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _scope() -> tuple[sa.Column, ...]:
    return (
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", Uuid16(), primary_key=True),
        sa.Column(
            "actor_scope", sa.LargeBinary(32).with_variant(BINARY(32), "mysql"), primary_key=True
        ),
    )


def upgrade() -> None:
    op.create_table(
        "studio_creator_installations",
        *_scope(),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("latest_version", sa.Integer(), nullable=False),
        sa.Column("selected_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "studio_creator_packages",
        *_scope(),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("digest", sa.LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" and not getattr(
        bind.connection.driver_connection, "in_transaction", False
    ):
        bind.exec_driver_sql("BEGIN")
    if bind.scalar(sa.text("SELECT 1 FROM studio_creator_packages LIMIT 1")):
        raise RuntimeError("Cannot downgrade while Creator panel packages exist")
    op.drop_table("studio_creator_packages")
    op.drop_table("studio_creator_installations")
