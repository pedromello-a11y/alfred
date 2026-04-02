"""Nudge quando task ativa passa de 2h sem atualização."""
from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Task
from app.services import task_manager, whapi_client


async def run() -> None:
    """Checks every 30min if there's a task active for 2+ hours without update."""
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return

            task_id, elapsed = await task_manager.get_active_task_elapsed_minutes(db)
            if not task_id or elapsed < 120:
                return

            result = await db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task or task.status != "in_progress":
                return

            hours = elapsed // 60
            mins = elapsed % 60
            msg = f"Ainda em *{task.title}* ou mudou de plano? ({hours}h{mins:02d}min)"
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
            logger.info("Timer nudge sent for task {} ({}min)", task.title, elapsed)
    except Exception as exc:
        logger.error("task_timer_nudge.run failed: {}", exc)
