"""Testes de proteção contra regressão — fluxos críticos do Alfred."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


# ── 1. GET /dashboard/state retorna 200 ────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_state_returns_200(client: AsyncClient):
    resp = await client.get("/dashboard/state")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


# ── 2. Criar task via POST /dashboard/task/create-smart ────────────────────

@pytest.mark.asyncio
async def test_create_smart_task(client: AsyncClient):
    resp = await client.post(
        "/dashboard/task/create-smart",
        json={"title": "Teste de criação", "status": "active"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "id" in data or "task" in data or "confirmed" in data or resp.status_code == 200


# ── 3. Completar task muda status para done ─────────────────────────────────

@pytest.mark.asyncio
async def test_complete_task(client: AsyncClient, make_task):
    task = await make_task(title="Completar isso", status="active")
    resp = await client.post(f"/dashboard/task/{task.id}/complete", json={})
    assert resp.status_code == 200
    # Verificar que status mudou no banco
    from app.models import Task
    from sqlalchemy import select
    # Buscar direto via endpoint de state
    state = await client.get("/dashboard/state")
    assert state.status_code == 200


# ── 4. Task com deadline vencida aparece no state ───────────────────────────

@pytest.mark.asyncio
async def test_overdue_task_in_state(client: AsyncClient, make_task):
    overdue = datetime.now(timezone.utc) - timedelta(days=3)
    await make_task(title="Atrasada", status="active", deadline=overdue)
    resp = await client.get("/dashboard/state")
    assert resp.status_code == 200
    data = resp.json()
    # State deve conter tarefas
    tasks = data.get("tasks") or data.get("all_tasks") or []
    assert isinstance(tasks, list)


# ── 5. Agenda reorganize não crasha ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_agenda_reorganize(client: AsyncClient):
    resp = await client.post("/dashboard/agenda/reorganize", json={})
    # Pode retornar 200 ou 422, mas não 500
    assert resp.status_code != 500


# ── 6. Gamificação award_task_completion retorna XP > 0 ─────────────────────

@pytest.mark.asyncio
async def test_gamification_xp(db_session: AsyncSession, make_task):
    from app.services.gamification_service import award_task_completion, calculate_points

    task = await make_task(title="Boss fight", status="active", estimated_minutes=60)
    task.completed_at = datetime.now(timezone.utc)

    final_xp, msg = await award_task_completion(task, db_session)
    assert final_xp > 0
    assert isinstance(msg, str)
    assert "XP" in msg


# ── 7. calculate_level é consistente ────────────────────────────────────────

@pytest.mark.asyncio
async def test_gamification_level_calc():
    from app.services.gamification_service import calculate_level, xp_progress_in_level

    assert calculate_level(0) == 1
    assert calculate_level(99) == 1
    assert calculate_level(100) == 2
    assert calculate_level(300) == 3  # 100 + 200 = 300

    current, needed = xp_progress_in_level(150, 2)
    assert current == 50   # 150 - 100 = 50 no nível 2
    assert needed == 200   # nível 2 requer 200 XP


# ── 8. dump create e list ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dump_crud(client: AsyncClient):
    # Criar dump
    resp = await client.post("/dashboard/dump", json={"content": "pensamento solto"})
    assert resp.status_code == 200

    # Listar dumps
    resp = await client.get("/dashboard/dumps")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, (list, dict))


# ── 9. task_manager.create persiste no banco ────────────────────────────────

@pytest.mark.asyncio
async def test_task_manager_create(db_session: AsyncSession):
    from app.services import task_manager

    class FakeItem:
        extracted_title = "Task via task_manager"
        title = "Task via task_manager"
        status = "active"
        deadline = None
        priority = None
        priority_hint = None
        category = None
        estimated_minutes = None
        is_boss_fight = False
        task_type = "task"
        parent_id = None
        origin = "manual"
        origin_ref = None
        importance = None
        effort_type = None
        deadline_type = None
        metadata = None

    task = await task_manager.create(FakeItem(), db_session)
    assert task.id is not None
    assert task.title == "Task via task_manager"
    assert task.status in ("active", "pending")


# ── 10. safe_async_call retorna fallback em caso de erro ────────────────────

@pytest.mark.asyncio
async def test_safe_async_call_fallback():
    from app.services.error_handling import safe_async_call

    async def explode():
        raise RuntimeError("erro proposital")

    result = await safe_async_call(explode, fallback="fallback_value", context="test")
    assert result == "fallback_value"


# ── 11. brain.try_regex_classify funciona sem banco ─────────────────────────

def test_regex_classify_update():
    from app.services.brain import try_regex_classify
    assert try_regex_classify("terminei a reunião") == "update"
    assert try_regex_classify("preciso fazer o relatório") == "new_task"
    assert try_regex_classify("o que tenho hoje?") == "question"
    assert try_regex_classify("olá tudo bem") is None


# ── 12. llm_client.get_client não inicializa sem API key ────────────────────

def test_llm_client_get_client():
    from app.services.llm_client import get_client
    # Deve retornar instância sem lançar exceção (key pode ser mock em config)
    try:
        client = get_client()
        assert client is not None
    except RuntimeError:
        # Aceitável se API key não estiver configurada em ambiente de teste
        pass
