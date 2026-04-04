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
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocked BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocked_reason VARCHAR")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocked_until VARCHAR")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP WITH TIME ZONE")
    op.execute("""
        CREATE TABLE IF NOT EXISTS work_days (
            id VARCHAR(36) PRIMARY KEY,
            date DATE NOT NULL UNIQUE,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            summary TEXT,
            energy_level INTEGER,
            tasks_completed INTEGER NOT NULL DEFAULT 0,
            total_minutes INTEGER NOT NULL DEFAULT 0
        )
    """)


def downgrade() -> None:
    op.drop_table("work_days")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("blocked_at")
        batch_op.drop_column("blocked_until")
        batch_op.drop_column("blocked_reason")
        batch_op.drop_column("blocked")
