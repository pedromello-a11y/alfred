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
    op.create_table(
        "schedule_blocks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False, server_default="default"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("block_type", sa.String(30), nullable=False, server_default="other"),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_fixed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_schedule_blocks_date", "schedule_blocks", ["date"])
    op.create_index("idx_schedule_blocks_user_id", "schedule_blocks", ["user_id"])

    op.create_table(
        "personal_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False, server_default="default"),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("done_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_personal_items_user_position", "personal_items", ["user_id", "position"])


def downgrade() -> None:
    op.drop_index("idx_personal_items_user_position", "personal_items")
    op.drop_table("personal_items")
    op.drop_index("idx_schedule_blocks_user_id", "schedule_blocks")
    op.drop_index("idx_schedule_blocks_date", "schedule_blocks")
    op.drop_table("schedule_blocks")
