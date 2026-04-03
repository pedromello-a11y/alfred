"""unify blocked columns and work_days table

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("blocked", sa.Boolean(), nullable=False, server_default="false"))
        batch_op.add_column(sa.Column("blocked_reason", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("blocked_until", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("blocked_at", sa.DateTime(), nullable=True))

    op.create_table(
        "work_days",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("energy_level", sa.Integer(), nullable=True),
        sa.Column("tasks_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_minutes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("work_days")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("blocked_at")
        batch_op.drop_column("blocked_until")
        batch_op.drop_column("blocked_reason")
        batch_op.drop_column("blocked")
