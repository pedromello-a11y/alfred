import os
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("WHAPI_TOKEN", "test-whapi")
os.environ.setdefault("MY_WHATSAPP", "5511999999999")


# Patch: SQLite does not support JSONB - use JSON for tests
import sqlalchemy.dialects.postgresql as _pg
from sqlalchemy import JSON as _JSON
_pg.JSONB = _JSON

from app import database as app_database
from app.database import Base
from app.services import brain, interpreter, runtime_router


def _to_async_db_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(autouse=True)
def stub_external_calls(monkeypatch):
    async def fake_classify(text: str, db=None):
        return {"classification": "chat", "extracted_title": text[:80], "extracted_deadline": None, "priority_hint": None}

    async def fake_answer_question(question: str, context: str, db=None):
        return f"[fake-answer] {question}"

    async def fake_casual_response(message: str, db=None):
        return f"[fake-casual] {message[:80]}"

    async def fake_execute_command(command: str, context: str, db=None):
        return f"[fake-command] {command[:80]}"

    async def fake_interpret_message(text: str, db=None):
        return None

    monkeypatch.setattr(brain, "classify", fake_classify)
    monkeypatch.setattr(brain, "answer_question", fake_answer_question)
    monkeypatch.setattr(brain, "casual_response", fake_casual_response)
    monkeypatch.setattr(brain, "execute_command", fake_execute_command)
    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)


@pytest.fixture
async def db_session(monkeypatch):
    database_url = _to_async_db_url(os.environ["DATABASE_URL"])
    engine = create_async_engine(database_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(app_database, "engine", engine)
    monkeypatch.setattr(app_database, "AsyncSessionLocal", session_factory)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def send(db_session):
    async def _send(text: str):
        item, response, classification = await runtime_router.handle(text, db=db_session)
        return item, response, classification

    return _send
