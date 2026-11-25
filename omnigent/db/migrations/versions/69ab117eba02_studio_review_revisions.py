"""Store the automation queue, frozen revision inputs and decision history."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "69ab117eba02"
down_revision = "71208e0fd432"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "studio_review_revisions",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("repository", sa.String(256), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("expires_at", sa.BigInteger()),
        sa.Column("capture_json", sa.Text(), nullable=False),
        sa.Column("active_slot", sa.Integer()),
        sa.UniqueConstraint(
            "workspace_id", "repository", "active_slot", name="uq_studio_review_active_repo"
        ),
        sa.Column("result_json", sa.Text()),
        sa.Column("decisions_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_studio_review_queue",
        "studio_review_revisions",
        ["workspace_id", "repository", "created_at", "id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT 1 FROM studio_review_revisions LIMIT 1")):
        raise RuntimeError("Export existing Studio review receipts before downgrade")
    op.drop_table("studio_review_revisions")
