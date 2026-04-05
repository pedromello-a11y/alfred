from collections.abc import AsyncGenerator

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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


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

    await _seed_data()


async def _seed_data() -> None:
    """Seed inicial com upsert batch — idempotente via ON CONFLICT DO NOTHING."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models import Achievement, PlayerStat

    async with AsyncSessionLocal() as db:
        for attr in _PLAYER_STAT_ATTRIBUTES:
            stmt = (
                pg_insert(PlayerStat.__table__)
                .values(attribute=attr, xp=0, level=1, prestige=0)
                .on_conflict_do_nothing(index_elements=["attribute"])
            )
            await db.execute(stmt)

        for code, name, desc in _ACHIEVEMENTS:
            stmt = (
                pg_insert(Achievement.__table__)
                .values(code=code, name=name, description=desc)
                .on_conflict_do_nothing(index_elements=["code"])
            )
            await db.execute(stmt)

        await db.commit()