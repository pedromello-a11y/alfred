from datetime import datetime, time

from sqlalchemy import select

from app.models import AgendaBlock, DumpItem, Task
from app.services import dump_manager, interpreter, message_handler, runtime_router, task_manager
from app.services.time_utils import today_brt


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


async def test_interpreter_system_feedback_does_not_create_task(db_session, monkeypatch):
    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "system_feedback",
            "confidence": 0.97,
            "raw_text": text,
            "note": text,
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("isso não era pra virar task, era ajuste do sistema", db=db_session)
    task_result = await db_session.execute(select(Task))
    tasks = list(task_result.scalars().all())
    saved = await task_manager.get_setting("last_system_feedback", db=db_session)

    assert classification == "system_feedback"
    assert len(tasks) == 0
    assert "ajuste de comportamento do sistema" in response.lower()
    assert saved is not None and "ajuste do sistema" in saved.lower()


async def test_interpreter_context_note_can_attach_to_named_task(db_session, monkeypatch):
    task = await task_manager.upsert_task_from_context("FIRE 26 | Abertura", db_session, status="in_progress", category="work")

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "context_note",
            "confidence": 0.96,
            "raw_text": text,
            "reference_title": "Abertura",
            "note": "O Cavazza quer manter o corte atual.",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("nota: o Cavazza quer manter o corte atual", db=db_session)
    result = await db_session.execute(select(Task).where(Task.id == task.id))
    refreshed = result.scalar_one()

    assert classification == "context_note"
    assert "guardei isso como nota" in response.lower()
    assert refreshed.notes is not None and "cavazza" in refreshed.notes.lower()


async def test_agenda_question_uses_runtime_router_not_legacy_handler(db_session, monkeypatch):
    today = today_brt()
    block = AgendaBlock(
        title="Reunião com Bárbara",
        start_at=datetime.combine(today, time(15, 0)),
        end_at=datetime.combine(today, time(16, 0)),
        block_type="meeting",
        source="manual",
    )
    db_session.add(block)
    await db_session.commit()
    await task_manager.upsert_task_from_context("Spark | Countdown", db_session, status="in_progress", category="work")

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "question",
            "confidence": 0.95,
            "raw_text": text,
        }

    async def fail_legacy(*args, **kwargs):
        raise AssertionError("legacy handler should not run for agenda question")

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)
    monkeypatch.setattr(message_handler, "handle", fail_legacy)

    _, response, classification = await runtime_router.handle("como está minha agenda hoje?", db=db_session)

    assert classification == "question"
    assert "Agenda de hoje" in response
    assert "Reunião com Bárbara" in response
    assert "Agora:" in response


async def test_focus_question_uses_runtime_router(db_session, monkeypatch):
    await task_manager.upsert_task_from_context("FIRE 26 | Ajustar abertura", db_session, status="in_progress", category="work")

    async def fake_interpret_message(text: str, db=None):
        return {"intent": "question", "confidence": 0.95, "raw_text": text}

    async def fail_legacy(*args, **kwargs):
        raise AssertionError("legacy handler should not run for focus question")

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)
    monkeypatch.setattr(message_handler, "handle", fail_legacy)

    _, response, classification = await runtime_router.handle("qual meu foco agora?", db=db_session)

    assert classification == "question"
    assert "foco" in response.lower()
    assert "ajustar abertura" in response.lower()


async def test_unknown_agenda_input_falls_back_without_legacy(db_session, monkeypatch):
    async def fake_interpret_message(text: str, db=None):
        return {"intent": "unknown", "confidence": 0.91, "raw_text": text}

    async def fail_legacy(*args, **kwargs):
        raise AssertionError("legacy handler should not run for unknown agenda fallback")

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)
    monkeypatch.setattr(message_handler, "handle", fail_legacy)

    _, response, classification = await runtime_router.handle("amanhã 15h reunião com a Bárbara por 1h", db=db_session)
    result = await db_session.execute(select(AgendaBlock))
    blocks = list(result.scalars().all())

    assert classification == "agenda_add_fallback"
    assert len(blocks) == 1
    assert "Agenda registrada" in response


async def test_interpreter_correction_prefers_named_dump_when_multiple_exist(db_session, monkeypatch):
    dump_a = await dump_manager.create_dump_item("quero ver o filme Pulp Fiction", "whatsapp", db_session)
    dump_b = await dump_manager.create_dump_item("lembrar de comprar tinta dourada", "whatsapp", db_session)
    await task_manager.set_setting("last_action_type", "dump", db_session)
    await task_manager.set_setting("last_action_id", str(dump_a.id), db_session)

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.95,
            "raw_text": text,
            "correction_new_type": "task",
            "reference_title": "tinta dourada",
            "task_title": "Comprar tinta dourada",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("isso de tinta dourada é tarefa", db=db_session)
    task_result = await db_session.execute(select(Task))
    dump_result = await db_session.execute(select(DumpItem))
    tasks = list(task_result.scalars().all())
    dumps = list(dump_result.scalars().all())

    assert classification == "correction"
    assert any("tinta dourada" in (t.title or "").lower() for t in tasks)
    assert any("pulp fiction" in ((d.raw_text or '') + ' ' + (d.rewritten_title or '')).lower() for d in dumps)
    assert not any("tinta dourada" in ((d.raw_text or '') + ' ' + (d.rewritten_title or '')).lower() for d in dumps)
    assert "Transformei o dump em task" in response


async def test_interpreter_correction_prefers_named_agenda_block_when_multiple_exist(db_session, monkeypatch):
    today = today_brt()
    block_a = AgendaBlock(
        title="Reunião com Bárbara",
        start_at=datetime.combine(today, time(15, 0)),
        end_at=datetime.combine(today, time(16, 0)),
        block_type="meeting",
        source="manual",
    )
    block_b = AgendaBlock(
        title="Reunião com Cavazza",
        start_at=datetime.combine(today, time(17, 0)),
        end_at=datetime.combine(today, time(18, 0)),
        block_type="meeting",
        source="manual",
    )
    db_session.add(block_a)
    db_session.add(block_b)
    await db_session.commit()
    await task_manager.set_setting("last_action_type", "agenda_block", db_session)
    await task_manager.set_setting("last_action_id", str(block_a.id), db_session)

    async def fake_interpret_message(text: str, db=None):
        return {
            "intent": "correction",
            "confidence": 0.95,
            "raw_text": text,
            "correction_new_type": "task",
            "reference_title": "Cavazza",
            "task_title": "Preparar reunião com Cavazza",
        }

    monkeypatch.setattr(interpreter, "interpret_message", fake_interpret_message)

    _, response, classification = await runtime_router.handle("a do cavazza era tarefa", db=db_session)
    task_result = await db_session.execute(select(Task))
    block_result = await db_session.execute(select(AgendaBlock))
    tasks = list(task_result.scalars().all())
    blocks = list(block_result.scalars().all())

    assert classification == "correction"
    assert any("cavazza" in (t.title or "").lower() for t in tasks)
    assert any("barbara" in (b.title or "").lower() for b in blocks)
    assert not any("cavazza" in (b.title or "").lower() for b in blocks)
    assert "Transformei o bloco de agenda em task" in response
