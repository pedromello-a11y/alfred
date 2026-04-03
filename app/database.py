from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


db_url = settings.database_url
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(db_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


_PLAYER_STAT_ATTRIBUTES = [
    "craft", "strategy", "life", "willpower", "knowledge", "recovery"
]

_ACHIEVEMENTS = [
    ("first_blood",   "Primeiro sangue",  "Primeira tarefa do dia concluída antes das 9h"),
    ("combo_x3",      "Combo x3",         "3 tarefas concluídas em sequência sem pausa >10min"),
    ("slayer",        "Slayer",           "Boss fight derrotado na primeira tentativa"),
    ("early_bird",    "Madrugador",       "5 dias seguidos começando antes das 9h"),
    ("perfect_day",   "Zerou o dia",      "100% do plano diário concluído"),
    ("phoenix",       "Fênix",            "Retomou produtividade após 3+ dias de inatividade"),
    ("sniper",        "Sniper",           "Tarefa concluída em menos da metade do tempo estimado"),
    ("balanced",      "Equilibrista",     "Tarefas work E personal no mesmo dia por 5 dias"),
    ("archaeologist", "Arqueólogo",       "Concluiu tarefa do backlog com mais de 30 dias"),
    ("ghost",         "Ghost",            "Dia inteiro produtivo sem pedir ajuda de destravamento"),
]


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _migrate_v3_columns()
    await _seed_data()


async def _migrate_v3_columns() -> None:
    """Adiciona colunas v3 na tabela tasks — idempotente via IF NOT EXISTS."""
    from sqlalchemy import text
    stmts = [
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS checklist_json JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notes_json JSONB DEFAULT '[]'::jsonb",
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_type VARCHAR(10) DEFAULT 'soft'",
        """
        CREATE TABLE IF NOT EXISTS work_days (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            date DATE NOT NULL UNIQUE,
            started_at TIMESTAMPTZ,
            ended_at TIMESTAMPTZ,
            summary_json JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """,
    ]
    try:
        async with engine.begin() as conn:
            for sql in stmts:
                await conn.execute(text(sql))
    except Exception as exc:
        # Não interrompe o startup se o banco não suportar (ex: SQLite em testes)
        import logging
        logging.getLogger(__name__).warning("_migrate_v3_columns: %s", exc)


async def _seed_data() -> None:
    """Seed inicial: player_stats e achievements (idempotente via INSERT OR IGNORE logic)."""
    from sqlalchemy import select
    from app.models import Achievement, PlayerStat

    async with AsyncSessionLocal() as db:
        for attribute in _PLAYER_STAT_ATTRIBUTES:
            result = await db.execute(
                select(PlayerStat).where(PlayerStat.attribute == attribute)
            )
            if result.scalar_one_or_none() is None:
                db.add(PlayerStat(attribute=attribute, xp=0, level=1, prestige=0))

        for code, name, description in _ACHIEVEMENTS:
            result = await db.execute(
                select(Achievement).where(Achievement.code == code)
            )
            if result.scalar_one_or_none() is None:
                db.add(Achievement(code=code, name=name, description=description))

        await db.commit()