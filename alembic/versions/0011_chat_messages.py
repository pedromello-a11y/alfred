"""add chat_messages table

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-05
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            role VARCHAR(20) NOT NULL,
            content TEXT NOT NULL,
            intent VARCHAR(50),
            result_data JSONB,
            created_at TIMESTAMP DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages (created_at)")


def downgrade() -> None:
    op.drop_table("chat_messages")
