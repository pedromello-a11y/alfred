from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Task
from app.services import task_manager, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = list(result.scalars().all())
            active = list(await task_manager.get_active_tasks(db))
            if done_today:
                next_focus = active[0].title if active else "nenhum foco pendente"
                msg = f"Bom ritmo. Você já concluiu {len(done_today)} hoje. Próximo foco: *{next_focus}*."
            else:
                top = active[0].title if active else "sua tarefa principal"
                msg = f"Como está o dia? Se travou, foca só em *{top}* por 5 minutos."
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("midday_checkin.run failed: {}", exc)


async def run_plan_b() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = list(result.scalars().all())
            if done_today:
                return
            victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
            active = list(await task_manager.get_active_tasks(db))
            title = active[0].title if active else "sua tarefa principal"
            if victory_id:
                try:
                    victory_task = next(t for t in active if str(t.id) == victory_id)
                    title = victory_task.title
                except StopIteration:
                    pass
            msg = f"Plano B: com o tempo que resta, foca só em *{title}*. O resto vai pra amanhã, sem culpa."
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("midday_checkin.run_plan_b failed: {}", exc)
