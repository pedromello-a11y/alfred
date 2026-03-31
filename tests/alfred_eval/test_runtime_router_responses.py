from datetime import datetime, timedelta

from app.services import interpreter, runtime_router, task_manager
from app.services.time_utils import now_brt_naive


async def test_new_task_response_includes_deadline_and_focus_hint(db_session, monkeypatch):
    async def fake_interpret_message(text: str, db=None):
        deadline = (now_brt_naive() + timedelta(hours=4)).isoformat()
        return {
            "intent": "new_task",
            "confidence": 0.95,
            "task_title": "Fechar roteiro do Galaxy",
            "project": "Galaxy",
            "deadline_iso": deadline,
            "category": "work",
            "raw_text": text,
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("fechar roteiro do galaxy hoje", db=db_session)

    assert classification == "new_task"
    assert "Prazo:" in response
    assert "Próximo foco sugerido:" in response or "Foco agora:" in response


async def test_task_update_done_response_points_to_next_focus(db_session, monkeypatch):
    first = await task_manager.upsert_task_from_context("Galaxy | Fechar roteiro", db_session, status="in_progress", category="work")
    await task_manager.upsert_task_from_context("Galaxy | Revisar cortes", db_session, status="pending", category="work")

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "task_update",
            "confidence": 0.95,
            "reference_title": "Fechar roteiro",
            "task_status": "done",
            "raw_text": text,
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("fechei o roteiro", db=db_session)

    assert classification == "task_update"
    assert "concluída" in response
    assert "Revisar cortes" in response or "Próximo foco sugerido:" in response
