"""Fixtures compartilhadas para testes do Alfred."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import JSON, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base, get_db

# Substituir JSONB por JSON para SQLite
# Necessário porque models.py usa JSONB (PostgreSQL-only)
from sqlalchemy.dialects import postgresql
postgresql.JSONB = JSON  # type: ignore[attr-defined]

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine_test = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSession = async_sessionmaker(engine_test, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Cria tabelas antes de cada teste."""
    # Import models para registrar no Base.metadata
    import app.models  # noqa: F401
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine_test.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    async with TestSession() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncClient:
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def make_task(db_session: AsyncSession):
    async def _make(title: str = "Tarefa teste", status: str = "active", **kwargs):
        from app.models import Task
        task = Task(
            id=uuid4(),
            title=title,
            status=status,
            task_type=kwargs.pop("task_type", "task"),
            **kwargs,
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        return task
    return _make
