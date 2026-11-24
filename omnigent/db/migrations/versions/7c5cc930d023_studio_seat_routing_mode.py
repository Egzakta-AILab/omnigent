"""Keep a seat's fallback routing choice across native dispatches."""

import sqlalchemy as sa
from alembic import op

revision = "7c5cc930d023"
down_revision = "69ab117eba02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studio_seat_slots",
        sa.Column("routing_mode", sa.String(16), nullable=False, server_default="subscription"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT 1 FROM studio_seat_slots WHERE routing_mode != 'subscription' LIMIT 1")
    ):
        raise RuntimeError("Restore fallback slots to subscription routing before downgrade")
    with op.batch_alter_table("studio_seat_slots") as batch:
        batch.drop_column("routing_mode")
