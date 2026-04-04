"""add schedule_blocks and personal_items tables

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS schedule_blocks (
            id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL DEFAULT 'default',
            title VARCHAR(200) NOT NULL,
            block_type VARCHAR(30) NOT NULL DEFAULT 'other',
            date DATE NOT NULL,
            start_time TIME NOT NULL,
            end_time TIME NOT NULL,
            is_fixed BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_schedule_blocks_date ON schedule_blocks (date)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_schedule_blocks_user_id ON schedule_blocks (user_id)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS personal_items (
            id UUID PRIMARY KEY,
            user_id VARCHAR NOT NULL DEFAULT 'default',
            title VARCHAR(500) NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            done BOOLEAN NOT NULL DEFAULT false,
            done_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_personal_items_user_position ON personal_items (user_id, position)")


def downgrade() -> None:
    op.drop_index("idx_personal_items_user_position", "personal_items")
    op.drop_table("personal_items")
    op.drop_index("idx_schedule_blocks_user_id", "schedule_blocks")
    op.drop_index("idx_schedule_blocks_date", "schedule_blocks")
    op.drop_table("schedule_blocks")
