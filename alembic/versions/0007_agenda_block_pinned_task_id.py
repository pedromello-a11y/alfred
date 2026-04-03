"""add pinned and task_id to agenda_blocks

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agenda_blocks") as batch_op:
        batch_op.add_column(sa.Column("task_id", sa.UUID(), nullable=True))
        batch_op.add_column(sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false"))
    op.create_index("idx_agenda_blocks_task_id", "agenda_blocks", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_agenda_blocks_task_id", "agenda_blocks")
    with op.batch_alter_table("agenda_blocks") as batch_op:
        batch_op.drop_column("pinned")
        batch_op.drop_column("task_id")
