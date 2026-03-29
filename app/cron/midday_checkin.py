"""
midday_checkin.py — 13:00 e 14:00 seg-sex
Check-in contextual: verifica atividade desde o briefing das 09:00.
- Se nenhuma atividade em >3h: perguntar sobre tarefa #1
- Se houve atividade: elogiar + sugerir próxima
- Plano B: 14h e nenhuma tarefa concluída → foca só na vitória do dia
"""
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Message, Task
from app.services import task_manager, whapi_client

_INACTIVITY_THRESHOLD = timedelta(hours=3)


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            # Modo crise: não cobrar, não mandar check-in agressivo
            crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
            if crisis_mode == "true":
                return

            # Check-in do meio-dia conta no budget de interrupções proativas
            if not await task_manager.can_send_proactive(db):
                logger.info("Midday checkin skipped — proactive budget exhausted.")
                return

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            # Verificar se alguma tarefa foi concluída hoje
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = result.scalars().all()

            active = await _has_activity_since_briefing(db)
            if active:
                await _send_progress(db, done_today)
            else:
                await _send_inactivity_nudge(db)

    except Exception as exc:
        logger.error("midday_checkin.run failed: {}", exc)


async def run_plan_b() -> None:
    """14:00 seg-sex — Plano B: se nenhuma tarefa concluída, focar só na vitória do dia."""
    try:
        async with AsyncSessionLocal() as db:
            crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
            if crisis_mode == "true":
                return  # modo crise já simplifica o briefing

            if not await task_manager.can_send_proactive(db):
                logger.info("Plan B skipped — proactive budget exhausted.")
                return

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = result.scalars().all()

            if not done_today:
                await _send_plan_b(db)

    except Exception as exc:
        logger.error("midday_checkin.run_plan_b failed: {}", exc)


async def _has_activity_since_briefing(db) -> bool:
    """Verifica se houve mensagem inbound desde as 09:00 de hoje."""
    cutoff = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(Message)
        .where(Message.direction == "inbound")
        .where(Message.created_at >= cutoff)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _send_plan_b(db) -> None:
    """
    Plano B: nenhuma tarefa concluída até o check-in das 13h.
    Foca só na vitória do dia; o resto vai pra amanhã.
    """
    tasks = await task_manager.get_pending(db)
    n_total = len(tasks)

    victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
    victory_title = None
    if victory_id:
        result = await db.execute(
            select(Task).where(Task.id == victory_id)
        )
        victory = result.scalar_one_or_none()
        if victory:
            victory_title = victory.title

    if not victory_title and tasks:
        victory_title = tasks[0].title

    if not victory_title:
        victory_title = "sua tarefa principal"

    msg = (
        f"O plano original tinha {n_total} tarefa{'s' if n_total != 1 else ''}. "
        f"Com o tempo que resta, foco só em: *{victory_title}*.\n"
        f"O resto vai pra amanhã. Sem culpa. 🎯"
    )
    await whapi_client.send_message(settings.pedro_phone, msg)
    await task_manager.increment_proactive_count(db)
    logger.info("Midday plan B sent ({} tasks pending, victory: {}).", n_total, victory_title)


async def _send_inactivity_nudge(db) -> None:
    """Nenhuma atividade desde o briefing — perguntar sobre tarefa #1."""
    tasks = await task_manager.get_pending(db)
    tarefa_1 = tasks[0].title if tasks else "sua tarefa principal"
    msg = (
        f"E aí Pedro, como tá o dia? 👀\n"
        f"Vi que *{tarefa_1}* ainda tá pendente.\n"
        f"Travou em algo? Posso te ajudar a quebrar em pedaços menores."
    )
    await whapi_client.send_message(settings.pedro_phone, msg)
    await task_manager.increment_proactive_count(db)
    logger.info("Midday nudge sent (inactivity).")


async def _send_progress(db, done_today: list) -> None:
    """Houve atividade — contar tarefas concluídas hoje e sugerir próxima."""
    n = len(done_today)
    pending = await task_manager.get_pending(db)
    proxima = pending[0].title if pending else None

    if proxima:
        msg = f"Bom ritmo! ✅ Já fez {n} tarefa{'s' if n != 1 else ''} hoje.\n\nPróximo foco: *{proxima}*"
    else:
        msg = f"Dia limpo! ✅ Concluiu {n} tarefa{'s' if n != 1 else ''} hoje. Quer adiantar algo de amanhã ou descansar?"

    await whapi_client.send_message(settings.pedro_phone, msg)
    await task_manager.increment_proactive_count(db)
    logger.info("Midday progress sent ({} done today).", n)
