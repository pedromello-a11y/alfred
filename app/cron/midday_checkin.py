"""
midday_checkin.py — 13:00 seg-sex
Check-in contextual: verifica atividade desde o briefing das 09:00.
- Se nenhuma atividade em >3h: perguntar sobre tarefa #1
- Se houve atividade: elogiar + sugerir próxima
"""
from datetime import datetime, timedelta, timezone

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
            active = await _has_activity_since_briefing(db)

            if active:
                await _send_progress(db)
            else:
                await _send_inactivity_nudge(db)
    except Exception as exc:
        logger.error("midday_checkin.run failed: {}", exc)


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
    logger.info("Midday nudge sent (inactivity).")


async def _send_progress(db) -> None:
    """Houve atividade — contar tarefas concluídas hoje e sugerir próxima."""
    from datetime import date
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .where(Task.completed_at >= today_start)
    )
    done_today = result.scalars().all()
    n = len(done_today)

    pending = await task_manager.get_pending(db)
    proxima = pending[0].title if pending else None

    if proxima:
        msg = f"Bom ritmo! ✅ Já fez {n} tarefa{'s' if n != 1 else ''} hoje.\n\nPróximo foco: *{proxima}*"
    else:
        msg = f"Dia limpo! ✅ Concluiu {n} tarefa{'s' if n != 1 else ''} hoje. Quer adiantar algo de amanhã ou descansar?"

    await whapi_client.send_message(settings.pedro_phone, msg)
    logger.info("Midday progress sent ({} done today).", n)
