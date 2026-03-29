"""initial schema — todas as tabelas + seed

Revision ID: 0001
Revises:
Create Date: 2026-03-28

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # tasks
    # -----------------------------------------------------------------------
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.VARCHAR(500), nullable=False),
        sa.Column("description", sa.TEXT, nullable=True),
        sa.Column("origin", sa.VARCHAR(20), nullable=True),
        sa.Column("origin_ref", sa.VARCHAR(100), nullable=True),
        sa.Column("status", sa.VARCHAR(20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.INTEGER, nullable=True),
        sa.Column("category", sa.VARCHAR(20), nullable=True),
        sa.Column("deadline", sa.TIMESTAMP, nullable=True),
        sa.Column("estimated_minutes", sa.INTEGER, nullable=True),
        sa.Column("actual_minutes", sa.INTEGER, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP, nullable=True),
        sa.Column("notes", sa.TEXT, nullable=True),
        sa.Column("times_planned", sa.INTEGER, nullable=False, server_default="0"),
        sa.Column("last_planned", sa.DATE, nullable=True),
        sa.Column("is_boss_fight", sa.BOOLEAN, nullable=False, server_default="false"),
        sa.Column("importance", sa.INTEGER, nullable=True),
        sa.Column("effort_type", sa.VARCHAR(20), nullable=True),
    )
    op.create_index("idx_tasks_status_priority", "tasks", ["status", "priority"])
    op.create_index("idx_tasks_deadline", "tasks", ["deadline"])
    op.create_index("idx_tasks_category", "tasks", ["category"])

    # -----------------------------------------------------------------------
    # messages
    # -----------------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("direction", sa.VARCHAR(10), nullable=False),
        sa.Column("content", sa.TEXT, nullable=False),
        sa.Column("message_type", sa.VARCHAR(20), nullable=False),
        sa.Column("processed", sa.BOOLEAN, nullable=False, server_default="false"),
        sa.Column("classification", sa.VARCHAR(30), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.text("now()")),
    )
    op.create_index("idx_messages_created_at", "messages", ["created_at"])
    op.create_index("idx_messages_classification", "messages", ["classification"])

    # -----------------------------------------------------------------------
    # memories
    # -----------------------------------------------------------------------
    op.create_table(
        "memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("memory_type", sa.VARCHAR(20), nullable=False),
        sa.Column("content", sa.TEXT, nullable=False),
        sa.Column("period_start", sa.DATE, nullable=False),
        sa.Column("period_end", sa.DATE, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.text("now()")),
        sa.Column("superseded", sa.BOOLEAN, nullable=False, server_default="false"),
    )
    op.create_index("idx_memories_type_period", "memories", ["memory_type", "period_start"])

    # -----------------------------------------------------------------------
    # daily_plans
    # -----------------------------------------------------------------------
    op.create_table(
        "daily_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("plan_date", sa.DATE, nullable=False),
        sa.Column("plan_content", sa.TEXT, nullable=False),
        sa.Column("tasks_planned", postgresql.JSONB, nullable=True),
        sa.Column("tasks_completed", postgresql.JSONB, nullable=True),
        sa.Column("score", sa.INTEGER, nullable=True),
        sa.Column("consolidated", sa.BOOLEAN, nullable=False, server_default="false"),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.text("now()")),
        sa.UniqueConstraint("plan_date", name="uq_daily_plans_plan_date"),
    )

    # -----------------------------------------------------------------------
    # jira_cache
    # -----------------------------------------------------------------------
    op.create_table(
        "jira_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("jira_key", sa.VARCHAR(20), nullable=False),
        sa.Column("summary", sa.VARCHAR(500), nullable=False),
        sa.Column("status", sa.VARCHAR(50), nullable=True),
        sa.Column("priority", sa.VARCHAR(20), nullable=True),
        sa.Column("deadline", sa.TIMESTAMP, nullable=True),
        sa.Column("project_name", sa.VARCHAR(100), nullable=True),
        sa.Column("description_summary", sa.TEXT, nullable=True),
        sa.Column("last_synced", sa.TIMESTAMP, nullable=True),
        sa.UniqueConstraint("jira_key", name="uq_jira_cache_jira_key"),
    )

    # -----------------------------------------------------------------------
    # streaks
    # -----------------------------------------------------------------------
    op.create_table(
        "streaks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("streak_date", sa.DATE, nullable=False),
        sa.Column("tasks_completed", sa.INTEGER, nullable=False, server_default="0"),
        sa.Column("points", sa.INTEGER, nullable=False, server_default="0"),
        sa.Column("streak_count", sa.INTEGER, nullable=False, server_default="0"),
        sa.UniqueConstraint("streak_date", name="uq_streaks_streak_date"),
    )

    # -----------------------------------------------------------------------
    # api_usage
    # -----------------------------------------------------------------------
    op.create_table(
        "api_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("model", sa.VARCHAR(50), nullable=False),
        sa.Column("input_tokens", sa.INTEGER, nullable=False),
        sa.Column("output_tokens", sa.INTEGER, nullable=False),
        sa.Column("estimated_cost_usd", sa.FLOAT, nullable=False),
        sa.Column("call_type", sa.VARCHAR(30), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP, server_default=sa.text("now()")),
    )
    op.create_index("idx_api_usage_created_at", "api_usage", ["created_at"])

    # -----------------------------------------------------------------------
    # settings
    # -----------------------------------------------------------------------
    op.create_table(
        "settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.VARCHAR(100), nullable=False),
        sa.Column("value", sa.TEXT, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP, server_default=sa.text("now()")),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )

    # -----------------------------------------------------------------------
    # player_stats
    # -----------------------------------------------------------------------
    op.create_table(
        "player_stats",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("attribute", sa.VARCHAR(20), nullable=False),
        sa.Column("xp", sa.INTEGER, nullable=False, server_default="0"),
        sa.Column("level", sa.INTEGER, nullable=False, server_default="1"),
        sa.Column("prestige", sa.INTEGER, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP, server_default=sa.text("now()")),
        sa.UniqueConstraint("attribute", name="uq_player_stats_attribute"),
    )

    # -----------------------------------------------------------------------
    # achievements
    # -----------------------------------------------------------------------
    op.create_table(
        "achievements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.VARCHAR(50), nullable=False),
        sa.Column("name", sa.VARCHAR(100), nullable=False),
        sa.Column("description", sa.TEXT, nullable=True),
        sa.Column("unlocked_at", sa.TIMESTAMP, nullable=True),
        sa.UniqueConstraint("code", name="uq_achievements_code"),
    )

    # -----------------------------------------------------------------------
    # Seed: player_stats (6 atributos)
    # -----------------------------------------------------------------------
    op.execute("""
        INSERT INTO player_stats (attribute, xp, level, prestige)
        VALUES
            ('craft',      0, 1, 0),
            ('strategy',   0, 1, 0),
            ('life',       0, 1, 0),
            ('willpower',  0, 1, 0),
            ('knowledge',  0, 1, 0),
            ('recovery',   0, 1, 0)
        ON CONFLICT (attribute) DO NOTHING
    """)

    # -----------------------------------------------------------------------
    # Seed: achievements (10 conquistas)
    # -----------------------------------------------------------------------
    op.execute("""
        INSERT INTO achievements (code, name, description)
        VALUES
            ('first_blood',   'Primeiro sangue',  'Primeira tarefa do dia concluída antes das 9h'),
            ('combo_x3',      'Combo x3',          '3 tarefas concluídas em sequência sem pausa >10min'),
            ('slayer',        'Slayer',             'Boss fight derrotado na primeira tentativa'),
            ('early_bird',    'Madrugador',         '5 dias seguidos começando antes das 9h'),
            ('perfect_day',   'Zerou o dia',        '100% do plano diário concluído'),
            ('phoenix',       'Fênix',              'Retomou produtividade após 3+ dias de inatividade'),
            ('sniper',        'Sniper',             'Tarefa concluída em menos da metade do tempo estimado'),
            ('balanced',      'Equilibrista',       'Tarefas work E personal no mesmo dia por 5 dias'),
            ('archaeologist', 'Arqueólogo',         'Concluiu tarefa do backlog com mais de 30 dias'),
            ('ghost',         'Ghost',              'Dia inteiro produtivo sem pedir ajuda de destravamento')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("achievements")
    op.drop_table("player_stats")
    op.drop_table("settings")
    op.drop_table("api_usage")
    op.drop_table("streaks")
    op.drop_table("jira_cache")
    op.drop_table("daily_plans")
    op.drop_table("memories")
    op.drop_table("messages")
    op.drop_table("tasks")
