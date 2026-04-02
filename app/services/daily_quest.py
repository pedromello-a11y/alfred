"""Sistema de Daily Quest — missão secreta do dia."""
import random
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services import task_manager
from app.services.time_utils import today_brt

DAILY_QUESTS = [
    {"id": "early_victory", "label": "Completar a vitória do dia antes do almoço"},
    {"id": "fast_start", "label": "Começar a primeira tarefa em menos de 15min após o briefing"},
    {"id": "complete_personal", "label": "Completar pelo menos uma tarefa pessoal"},
    {"id": "zero_postponements", "label": "Não adiar nenhuma tarefa hoje"},
    {"id": "triple_combo", "label": "Completar 3 tarefas em sequência sem pausa longa"},
    {"id": "boss_attempt", "label": "Enfrentar pelo menos um boss fight"},
    {"id": "all_categories", "label": "Completar tarefas de pelo menos 2 categorias diferentes"},
    {"id": "under_estimate", "label": "Completar uma tarefa em menos tempo do que o estimado"},
]

QUEST_XP_BONUS = 50


async def generate_daily_quest(db: AsyncSession) -> dict:
    """Generates the daily quest and saves it to settings."""
    quest = random.choice(DAILY_QUESTS)
    await task_manager.set_setting("daily_quest_id", quest["id"], db)
    await task_manager.set_setting("daily_quest_label", quest["label"], db)
    logger.info("Daily quest generated: {}", quest["id"])
    return quest


async def check_daily_quest(db: AsyncSession) -> tuple[bool, str | None]:
    """Checks if the daily quest was completed. Returns (completed, quest_label)."""
    quest_id = await task_manager.get_setting("daily_quest_id", db=db)
    quest_label = await task_manager.get_setting("daily_quest_label", db=db)
    if not quest_id:
        return False, None

    today = today_brt()
    start = datetime.combine(today, datetime.min.time())

    done_result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .where(Task.completed_at >= start)
    )
    done_today = list(done_result.scalars().all())

    completed = False

    if quest_id == "early_victory":
        victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
        if victory_id:
            for t in done_today:
                # 15h UTC ≈ 12h BRT
                if str(t.id) == victory_id and t.completed_at and t.completed_at.hour < 15:
                    completed = True
                    break

    elif quest_id == "fast_start":
        briefing_sent = await task_manager.get_setting("briefing_sent_at", db=db)
        if briefing_sent and done_today:
            try:
                briefing_time = datetime.fromisoformat(briefing_sent)
                first_done = min(
                    (t for t in done_today if t.completed_at),
                    key=lambda t: t.completed_at,
                    default=None,
                )
                if first_done and first_done.completed_at:
                    if briefing_time.tzinfo is None:
                        from datetime import timezone
                        briefing_time = briefing_time.replace(tzinfo=timezone.utc)
                    diff = (first_done.completed_at - briefing_time).total_seconds()
                    if diff < 900:
                        completed = True
            except (ValueError, TypeError):
                pass

    elif quest_id == "complete_personal":
        completed = any(t.category == "personal" for t in done_today)

    elif quest_id == "zero_postponements":
        postponed = await task_manager.get_setting("tasks_postponed_today", "0", db=db)
        completed = int(postponed or "0") == 0

    elif quest_id == "triple_combo":
        if len(done_today) >= 3:
            sorted_done = sorted(
                [t for t in done_today if t.completed_at],
                key=lambda t: t.completed_at,
            )
            for i in range(len(sorted_done) - 2):
                t1 = sorted_done[i].completed_at
                t2 = sorted_done[i + 1].completed_at
                t3 = sorted_done[i + 2].completed_at
                if (
                    t1 and t2 and t3
                    and (t2 - t1).total_seconds() < 600
                    and (t3 - t2).total_seconds() < 600
                ):
                    completed = True
                    break

    elif quest_id == "boss_attempt":
        completed = any(t.is_boss_fight for t in done_today)

    elif quest_id == "all_categories":
        categories = {t.category for t in done_today if t.category}
        completed = len(categories) >= 2

    elif quest_id == "under_estimate":
        completed = any(
            t.estimated_minutes and t.actual_minutes and t.actual_minutes < t.estimated_minutes
            for t in done_today
        )

    return completed, quest_label
