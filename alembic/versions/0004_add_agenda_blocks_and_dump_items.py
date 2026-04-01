"""add agenda_blocks and dump_items tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agenda_blocks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("title", sa.VARCHAR(300), nullable=False),
        sa.Column("start_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("end_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("block_type", sa.VARCHAR(30), nullable=True, server_default="focus"),
        sa.Column("source", sa.VARCHAR(30), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=True, server_default="planned"),
        sa.Column("linked_task_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_agenda_blocks_start_at", "agenda_blocks", ["start_at"])
    op.create_index("idx_agenda_blocks_block_type", "agenda_blocks", ["block_type"])

    op.create_table(
        "dump_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("raw_text", sa.TEXT(), nullable=False),
        sa.Column("rewritten_title", sa.VARCHAR(500), nullable=False),
        sa.Column("summary", sa.TEXT(), nullable=True),
        sa.Column("category", sa.VARCHAR(80), nullable=True),
        sa.Column("subcategory", sa.VARCHAR(80), nullable=True),
        sa.Column("confidence", sa.FLOAT(), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=True, server_default="categorized"),
        sa.Column("source", sa.VARCHAR(30), nullable=True),
        sa.Column("source_task_id", sa.UUID(), nullable=True),
        sa.Column("notes", sa.TEXT(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_dump_items_category", "dump_items", ["category"])
    op.create_index("idx_dump_items_status", "dump_items", ["status"])
    op.create_index("idx_dump_items_created_at", "dump_items", ["created_at"])


def downgrade() -> None:
    op.drop_table("dump_items")
    op.drop_table("agenda_blocks")
