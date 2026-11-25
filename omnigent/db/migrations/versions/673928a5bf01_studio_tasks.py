"""Store shared issue anchors, inbox handoffs, dispatch leases and seat slots."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "673928a5bf01"
down_revision = "1b35d481df6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_tasks",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("linear_workspace", Uuid16(), nullable=False),
        sa.Column("linear_issue_id", Uuid16(), nullable=False),
        sa.Column("acceptance_check", sa.Text(), nullable=False),
        sa.Column("links_json", sa.Text(), nullable=False),
        sa.Column("confirmations_json", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "linear_workspace", "linear_issue_id", name="uq_studio_task_issue"
        ),
    )
    op.create_table(
        "studio_task_handoffs",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("request_id", Uuid16(), primary_key=True),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("sender", sa.String(128), nullable=False),
        sa.Column("sender_generation", sa.String(32)),
        sa.Column("recipient", sa.String(128), nullable=False),
        sa.Column("recipient_generation", sa.String(32)),
        sa.Column("packet_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text()),
    )
    op.create_index(
        "ix_studio_inbox_recipient",
        "studio_task_handoffs",
        ["workspace_id", "recipient", "request_id"],
    )
    op.create_table(
        "studio_agent_leases",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("idempotency_key", Uuid16(), primary_key=True),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("actor_generation", sa.String(32)),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("fix_round", sa.Integer(), nullable=False),
        sa.Column("lease_expires_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("fix_round BETWEEN 0 AND 3", name="ck_studio_lease_round"),
    )
    op.create_table(
        "studio_seat_slots",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("slot_id", sa.String(128), primary_key=True),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("encrypted_profile_ref", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.BigInteger(), nullable=False),
    )


def downgrade() -> None:
    tables = ("studio_seat_slots", "studio_agent_leases", "studio_task_handoffs", "studio_tasks")
    for table in tables:
        if op.get_bind().scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError("Export existing Studio task data before downgrade")
    for table in tables:
        op.drop_table(table)
