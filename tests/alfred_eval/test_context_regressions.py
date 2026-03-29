from sqlalchemy import select

from app.models import Task
from app.services import task_manager


SUMMARY_TEXT = """Esse é um resumo de demandas que tenho em aberto no trabalho

Demandas ativas agora
• Spark | Motion Avisos — em andamento
• Padrão de legendas para Cavazza — ativo
• Turntable do Cosmos 2 — pendente

Itens já resolvidos
• Spark | Countdown — concluído
• Spark | Screensaver — Rig já entregou

Detalhe: vídeo de abertura já foi startado, briefing enviado, 3 keyframes enviados e reunião com a 3K segunda às 11h."""


async def _get_titles(session, status: str | None = None):
    query = select(Task)
    if status:
        query = query.where(Task.status == status)
    result = await session.execute(query)
    return [t.title for t in result.scalars().all()]


async def test_context_summary_materializes_expected_tasks_without_note_pollution(db_session, send):
    _, response, classification = await send(SUMMARY_TEXT)

    assert classification == "context_update"
    assert "Spark | Motion Avisos" in response
    assert "Turntable do Cosmos 2" in response
    assert "Spark | Countdown" in response
    assert "Spark | Screensaver" in response
    assert "briefing →" not in response.lower()

    active_titles = await _get_titles(db_session, status="in_progress")
    pending_titles = await _get_titles(db_session, status="pending")
    done_titles = await _get_titles(db_session, status="done")

    assert "Spark | Motion Avisos" in active_titles
    assert "Padrão de legendas para Cavazza" in active_titles
    assert "Turntable do Cosmos 2" in pending_titles
    assert "Spark | Countdown" in done_titles
    assert "Spark | Screensaver" in done_titles

    all_titles = await _get_titles(db_session)
    assert "briefing" not in [t.lower() for t in all_titles]
    assert "entregue" not in [t.lower() for t in all_titles]
    assert not any("keyframe" in t.lower() for t in all_titles)

    result = await db_session.execute(select(Task).where(Task.title.ilike("%vídeo de abertura%") | Task.title.ilike("%video de abertura%")))
    video_task = result.scalar_one_or_none()
    assert video_task is not None
    assert video_task.status == "in_progress"
    assert video_task.notes is not None
    assert "3K" in video_task.notes or "3k" in video_task.notes.lower()


async def test_negation_does_not_mark_task_done_or_create_garbage_titles(db_session, send):
    await send("motion avisos está em andamento")
    _, response, _ = await send("quase terminei motion avisos, mas não terminei")

    assert "concluída" not in response.lower()

    result = await db_session.execute(select(Task).where(Task.title.ilike("%motion avisos%")))
    task = result.scalar_one_or_none()
    assert task is not None
    assert task.status == "in_progress"

    all_titles = [t.lower() for t in await _get_titles(db_session)]
    assert "quase" not in all_titles
    assert "isso" not in all_titles
    assert "entregue" not in all_titles
    assert not any(t.startswith("mas ainda") for t in all_titles)


async def test_system_backlog_does_not_leak_into_active_work_view(db_session, send):
    await task_manager.upsert_task_from_context(
        "áudio não funciona, adicionar na lista de ajustes do sistema",
        db_session,
        status="pending",
        category="system",
    )
    await send("motion avisos está ativo")

    _, response, _ = await send("minhas tarefas ativas")

    assert "Motion avisos" in response or "motion avisos" in response.lower()
    assert "áudio não funciona" not in response.lower()
    assert "audio nao funciona" not in response.lower()


async def test_aliases_consolidate_into_single_active_task(db_session, send):
    await send("motion avisos está ativo")
    await send("spark motion avisos está em andamento")
    await send("avisos do spark ainda estão rolando")

    result = await db_session.execute(select(Task))
    tasks = list(result.scalars().all())
    matching = [t for t in tasks if "motion avisos" in task_manager.normalize_task_title(t.title)]

    assert len(matching) == 1
    assert matching[0].status == "in_progress"
