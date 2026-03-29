"""add whapi_id to messages and context_sent to api_usage

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # messages.whapi_id — deduplicação de mensagens do Whapi
    op.add_column(
        "messages",
        sa.Column("whapi_id", sa.VARCHAR(100), nullable=True),
    )
    op.create_unique_constraint("uq_messages_whapi_id", "messages", ["whapi_id"])

    # api_usage.context_sent — auditoria do contexto enviado ao Claude
    op.add_column(
        "api_usage",
        sa.Column("context_sent", sa.TEXT, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_usage", "context_sent")
    op.drop_constraint("uq_messages_whapi_id", "messages", type_="unique")
    op.drop_column("messages", "whapi_id")
