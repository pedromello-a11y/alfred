import os
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Env mínimo para importar o app em modo teste
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_bootstrap.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("JIRA_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("JIRA_EMAIL", "test@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_REFRESH_TOKEN", "test-refresh")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")

from app import database as app_database
from app.database import Base
from app.services import brain, jira_client, message_handler


@pytest.fixture(autouse=True)
def stub_external_calls(monkeypatch):
    async def fake_classify(text: str, db=None):
        return {
            "classification": "chat",
            "extracted_title": text[:80],
            "extracted_deadline": None,
            "priority_hint": None,
        }

    async def fake_answer_question(question: str, context: str, db=None):
        return f"[fake-answer] {question}"

    async def fake_casual_response(message: str, db=None):
        return f"[fake-casual] {message[:80]}"

    async def fake_execute_command(command: str, context: str, db=None):
        return f"[fake-command] {command[:80]}"

    async def fake_get_cached_issues(db):
        return []

    monkeypatch.setattr(brain, "classify", fake_classify)
    monkeypatch.setattr(brain, "answer_question", fake_answer_question)
    monkeypatch.setattr(brain, "casual_response", fake_casual_response)
    monkeypatch.setattr(brain, "execute_command", fake_execute_command)
    monkeypatch.setattr(jira_client, "get_cached_issues", fake_get_cached_issues)


@pytest.fixture
async def db_session(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "alfred_eval.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    monkeypatch.setattr(app_database, "engine", engine)
    monkeypatch.setattr(app_database, "AsyncSessionLocal", session_factory)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def send(db_session):
    async def _send(text: str):
        item, response, classification = await message_handler.handle(text, db=db_session)
        return item, response, classification

    return _send
