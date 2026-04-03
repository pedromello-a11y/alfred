from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import app
from app.models import AgendaBlock, Task


@pytest.mark.anyio
async def test_dashboard_state_exposes_week_metadata_and_deadlines(db_session):
    task = Task(
        title="Meta | Subir meta",
        status="pending",
        category="work",
        deadline=datetime(2026, 4, 4, 0, 0),
    )
    db_session.add(task)
    await db_session.commit()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/dashboard/state")
        data = resp.json()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert data["agendaWeekStart"] == "2026-03-30"
    assert data["agendaWeekEnd"] == "2026-04-05"
    assert data["agendaDeadlines"] == [
        {
            "id": str(task.id),
            "title": "Meta | Subir meta",
            "project": "Meta",
            "taskName": "Subir meta",
            "date": "2026-04-04",
            "label": "04/04 00:00",
            "day": 5,
            "priority": None,
        }
    ]


@pytest.mark.anyio
async def test_dashboard_state_repairs_mojibake_in_agenda_titles(db_session):
    block = AgendaBlock(
        title="ReuniÃ£o com BÃ¡rbara",
        start_at=datetime(2026, 4, 3, 10, 0),
        end_at=datetime(2026, 4, 3, 11, 0),
        block_type="meeting",
        source="gcal",
        status="planned",
    )
    db_session.add(block)
    await db_session.commit()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/dashboard/state")
        data = resp.json()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    assert data["agenda"][4]["events"] == [
        {
            "title": "Reunião com Bárbara",
            "time": "10:00",
            "end": "11:00",
            "type": "meeting",
        }
    ]
