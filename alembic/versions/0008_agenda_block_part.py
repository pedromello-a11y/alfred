"""add part column to agenda_blocks for multi-day splits

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agenda_blocks") as batch_op:
        batch_op.add_column(sa.Column("part", sa.VARCHAR(10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agenda_blocks") as batch_op:
        batch_op.drop_column("part")
