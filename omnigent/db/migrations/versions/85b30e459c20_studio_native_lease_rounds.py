"""Persist private lease turn bindings and commit-pinned checks."""

import sqlalchemy as sa
from alembic import op

revision = "85b30e459c20"
down_revision = "7c5cc930d023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("studio_agent_leases", sa.Column("round_evidence_json", sa.Text()))


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_agent_leases WHERE round_evidence_json IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Export existing free-agent round evidence before downgrade")
    with op.batch_alter_table("studio_agent_leases") as batch:
        batch.drop_column("round_evidence_json")
