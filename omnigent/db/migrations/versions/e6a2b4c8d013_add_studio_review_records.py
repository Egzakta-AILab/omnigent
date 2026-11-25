"""Add owner-private review passes, idempotent operations and ordered events.

Revision ID: e6a2b4c8d013
Revises: d0a1f879a01d
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY, VARCHAR

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "e6a2b4c8d013"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the metadata-only review ledger without cross-database foreign keys."""
    digest = sa.LargeBinary(32).with_variant(BINARY(32), "mysql")
    op.create_table(
        "studio_review_passes",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("owner_scope", digest, nullable=False),
        sa.Column("owner_user_id", sa.String(256), nullable=True),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("target_json", CompressedText(), nullable=False),
        sa.Column("profile_json", CompressedText(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("dispatch_status", sa.SmallInteger(), nullable=False),
        sa.Column("reviewer_session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("state IN (1, 2, 3, 4, 5, 6)", name="ck_studio_review_pass_state"),
        sa.CheckConstraint(
            "dispatch_status IN (1, 2, 3, 4, 5, 6, 7)",
            name="ck_studio_review_dispatch_status",
        ),
    )
    op.create_index(
        "ix_studio_review_pass_owner_session",
        "studio_review_passes",
        ["workspace_id", "owner_scope", "source_session_id", "created_at", "id"],
    )
    op.create_table(
        "studio_review_operations",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("owner_scope", digest, nullable=False),
        sa.Column("review_id", Uuid16(), nullable=False),
        sa.Column("action", sa.SmallInteger(), nullable=False),
        sa.Column(
            "idempotency_key",
            sa.String(128).with_variant(VARCHAR(128, collation="utf8mb4_bin"), "mysql"),
            nullable=False,
        ),
        sa.Column("request_hash", digest, nullable=False),
        sa.Column("start_once", sa.SmallInteger(), nullable=True),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("action IN (1, 2, 3, 4, 5)", name="ck_studio_review_operation_action"),
        sa.CheckConstraint(
            "state IN (1, 2, 3, 4, 5, 6, 7)", name="ck_studio_review_operation_state"
        ),
        sa.CheckConstraint(
            "start_once IS NULL OR start_once = 1", name="ck_studio_review_start_once"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_scope",
            "action",
            "idempotency_key",
            name="uq_studio_review_operation_request",
        ),
        sa.UniqueConstraint(
            "workspace_id", "review_id", "start_once", name="uq_studio_review_operation_start"
        ),
    )
    op.create_table(
        "studio_review_events",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("review_id", Uuid16(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("operation_id", Uuid16(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    """Remove review metadata; conversation and artifact storage stays intact."""
    op.drop_table("studio_review_events")
    op.drop_table("studio_review_operations")
    op.drop_index("ix_studio_review_pass_owner_session", table_name="studio_review_passes")
    op.drop_table("studio_review_passes")
