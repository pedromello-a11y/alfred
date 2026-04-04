"""add task v3 fields — checklist_json, notes_json, deadline_type

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checklist_json JSONB DEFAULT '[]'")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notes_json JSONB DEFAULT '[]'")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_type VARCHAR(10) DEFAULT 'soft'")


def downgrade() -> None:
    op.drop_column("tasks", "deadline_type")
    op.drop_column("tasks", "notes_json")
    op.drop_column("tasks", "checklist_json")
