"""add unique constraint to settings.key

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-29

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remover duplicatas antes de adicionar constraint
    op.execute("""
        DELETE FROM settings a
        USING settings b
        WHERE a.id > b.id
          AND a.key = b.key
    """)
    op.create_unique_constraint("uq_settings_key", "settings", ["key"])


def downgrade() -> None:
    op.drop_constraint("uq_settings_key", "settings", type_="unique")
