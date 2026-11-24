"""Record workspace-scoped seat usage and capacity outcomes."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "71208e0fd432"
down_revision = "673928a5bf01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_api_budgets",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("budget_key", sa.String(96), primary_key=True),
        sa.Column("reserved_cents", sa.Integer(), nullable=False),
    )
    op.add_column("studio_agent_leases", sa.Column("session_id", Uuid16()))
    op.add_column("studio_agent_leases", sa.Column("session_start", sa.String(32)))
    op.create_table(
        "studio_dispatches",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("group_id", Uuid16()),
        sa.Column("elapsed_ms", sa.Integer()),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("slot_id", sa.String(128)),
        sa.Column("outcome", sa.String(24), nullable=False),
    )
    op.create_index(
        "ix_studio_dispatch_window", "studio_dispatches", ["workspace_id", "started_at"]
    )


def downgrade() -> None:
    if any(
        op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
        for table in ("studio_api_budgets", "studio_dispatches")
    ) or op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_agent_leases WHERE session_start IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Export existing Studio dispatch evidence before downgrade")
    op.drop_table("studio_api_budgets")
    op.drop_table("studio_dispatches")
    op.drop_column("studio_agent_leases", "session_start")
    op.drop_column("studio_agent_leases", "session_id")
