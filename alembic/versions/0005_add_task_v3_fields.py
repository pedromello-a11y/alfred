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
    op.add_column(
        "tasks",
        sa.Column("checklist_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default="[]"),
    )
    op.add_column(
        "tasks",
        sa.Column("notes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True, server_default="[]"),
    )
    op.add_column(
        "tasks",
        sa.Column("deadline_type", sa.VARCHAR(10), nullable=True, server_default="soft"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "deadline_type")
    op.drop_column("tasks", "notes_json")
    op.drop_column("tasks", "checklist_json")
