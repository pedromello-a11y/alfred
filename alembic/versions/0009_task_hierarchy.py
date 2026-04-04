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
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES tasks(id)")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type VARCHAR NOT NULL DEFAULT 'task'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks (parent_id)")


def downgrade() -> None:
    op.drop_index("idx_tasks_parent_id", "tasks")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("fk_tasks_parent_id", type_="foreignkey")
        batch_op.drop_column("task_type")
        batch_op.drop_column("parent_id")
