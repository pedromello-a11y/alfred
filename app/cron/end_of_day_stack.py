from datetime import date, datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Task
from app.services import task_manager, whapi_client
from app.services.tomorrow_board import build_tomorrow_board


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            today = date.today()
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= start)
            )
            done_today = list(result.scalars().all())
            pending = list(await task_manager.get_active_tasks(db))
            points = sum(task_manager.calculate_points(t) for t in done_today)
            tomorrow = await build_tomorrow_board(db)

            plan_result = await db.execute(select(DailyPlan).where(DailyPlan.plan_date == today))
            plan = plan_result.scalar_one_or_none()
            if plan:
                plan.consolidated = True
                plan.score = points
                plan.tasks_completed = {"ids": [str(t.id) for t in done_today]}
            await db.commit()

            due_tomorrow = tomorrow.get("dueTomorrow", [])
            next_hint = due_tomorrow[0]["title"] if due_tomorrow else (pending[0].title if pending else "nenhum foco definido")
            text = (
                f"Fechamento: {len(done_today)} concluídas hoje, "
                f"{len(pending)} abertas, {points} pontos. "
                f"Amanhã o primeiro foco sugerido é *{next_hint}*."
            )
            await whapi_client.send_message(settings.pedro_phone, text)
    except Exception as exc:
        logger.error("end_of_day_stack.run failed: {}", exc)
