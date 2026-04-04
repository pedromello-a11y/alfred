"""add parent_id and task_type to tasks for hierarchy

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("parent_id", sa.UUID(), nullable=True))
        batch_op.add_column(sa.Column("task_type", sa.String(), nullable=False, server_default="task"))
        batch_op.create_foreign_key("fk_tasks_parent_id", "tasks", ["parent_id"], ["id"])
    op.create_index("idx_tasks_parent_id", "tasks", ["parent_id"])


def downgrade() -> None:
    op.drop_index("idx_tasks_parent_id", "tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("fk_tasks_parent_id", type_="foreignkey")
        batch_op.drop_column("task_type")
        batch_op.drop_column("parent_id")
