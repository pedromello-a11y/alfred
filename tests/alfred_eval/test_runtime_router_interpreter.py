from sqlalchemy import select

from app.models import AgendaBlock, DumpItem, Task
from app.services import dump_manager, interpreter, runtime_router, task_manager


async def test_interpreter_new_task_creates_task_with_project(db_session, monkeypatch):
    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "new_task",
            "confidence": 0.95,
            "task_title": "alinhar motion do FIRE 26 com a Barbara",
            "project": "FIRE 26",
            "deadline_iso": None,
            "category": "work",
            "raw_text": text,
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("preciso alinhar motion do FIRE 26 com a Barbara", db=db_session)
    result = await db_session.execute(select(Task))
    tasks = list(result.scalars().all())

    assert classification == "new_task"
    assert len(tasks) == 1
    assert "FIRE 26" in tasks[0].title
    assert "Anotado" in response


async def test_interpreter_agenda_add_creates_structured_block(db_session, monkeypatch):
    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "agenda_add",
            "confidence": 0.93,
            "raw_text": text,
            "time_blocks": [
                {
                    "title": "Reunião com Bárbara",
                    "start_at": "2026-03-30T15:00:00-03:00",
                    "end_at": "2026-03-30T16:00:00-03:00",
                    "block_type": "meeting",
                }
            ],
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("amanhã 15h reunião com a Bárbara", db=db_session)
    result = await db_session.execute(select(AgendaBlock))
    blocks = list(result.scalars().all())

    assert classification == "agenda_add"
    assert len(blocks) == 1
    assert blocks[0].block_type == "meeting"
    assert "Agenda registrada" in response


async def test_interpreter_correction_moves_last_task_to_dump(db_session, monkeypatch):
    created = await task_manager.upsert_task_from_context("Pulp Fiction", db_session, status="pending", category="work")
    await task_manager.set_setting("last_action_type", "task", db_session)
    await task_manager.set_setting("last_action_id", str(created.id), db_session)

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.91,
            "raw_text": text,
            "correction_new_type": "dump",
            "reference_title": "Pulp Fiction",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("isso é dump", db=db_session)
    task_result = await db_session.execute(select(Task))
    dump_result = await db_session.execute(select(DumpItem))
    tasks = list(task_result.scalars().all())
    dumps = list(dump_result.scalars().all())

    assert classification == "correction"
    assert len(tasks) == 0
    assert len(dumps) == 1
    assert "Corrigido" in response


async def test_interpreter_correction_moves_last_dump_to_task(db_session, monkeypatch):
    dump = await dump_manager.create_dump_item("Dump: comprar tinta spray dourada", "whatsapp", db_session)
    await task_manager.set_setting("last_action_type", "dump", db_session)
    await task_manager.set_setting("last_action_id", str(dump.id), db_session)

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.92,
            "raw_text": text,
            "correction_new_type": "task",
            "task_title": "Comprar tinta spray dourada",
            "project": "Projeto X",
            "task_status": "pending",
            "category": "personal",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("isso é tarefa", db=db_session)
    task_result = await db_session.execute(select(Task))
    dump_result = await db_session.execute(select(DumpItem))
    tasks = list(task_result.scalars().all())
    dumps = list(dump_result.scalars().all())

    assert classification == "correction"
    assert len(tasks) == 1
    assert len(dumps) == 0
    assert "Projeto X" in tasks[0].title
    assert "Transformei o dump em task" in response


async def test_interpreter_correction_moves_last_task_to_agenda_block(db_session, monkeypatch):
    created = await task_manager.upsert_task_from_context("Reunião com equipe criativa", db_session, status="pending", category="work")
    await task_manager.set_setting("last_action_type", "task", db_session)
    await task_manager.set_setting("last_action_id", str(created.id), db_session)

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.94,
            "raw_text": text,
            "correction_new_type": "agenda_block",
            "time_blocks": [
                {
                    "title": "Reunião com equipe criativa",
                    "start_at": "2026-03-31T14:00:00-03:00",
                    "end_at": "2026-03-31T15:00:00-03:00",
                    "block_type": "meeting",
                }
            ],
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("isso era bloco na agenda", db=db_session)
    task_result = await db_session.execute(select(Task))
    block_result = await db_session.execute(select(AgendaBlock))
    tasks = list(task_result.scalars().all())
    blocks = list(block_result.scalars().all())

    assert classification == "correction"
    assert len(tasks) == 0
    assert len(blocks) == 1
    assert blocks[0].block_type == "meeting"
    assert "registrei isso na agenda" in response.lower()


async def test_interpreter_correction_can_target_named_task_not_only_last_action(db_session, monkeypatch):
    await task_manager.upsert_task_from_context("Spark | Countdown", db_session, status="pending", category="work")
    await task_manager.upsert_task_from_context("Spark | Motion Avisos", db_session, status="pending", category="work")

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.95,
            "raw_text": text,
            "correction_new_type": "dump",
            "reference_title": "Motion Avisos",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("motion avisos é dump", db=db_session)
    task_result = await db_session.execute(select(Task))
    dump_result = await db_session.execute(select(DumpItem))
    tasks = list(task_result.scalars().all())
    dumps = list(dump_result.scalars().all())

    assert classification == "correction"
    assert any("countdown" in (t.title or "").lower() for t in tasks)
    assert not any("motion avisos" in (t.title or "").lower() for t in tasks)
    assert any("motion avisos" in ((d.raw_text or '') + ' ' + (d.rewritten_title or '')).lower() for d in dumps)
