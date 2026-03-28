from datetime import date, datetime
from typing import Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services.message_handler import InboundItem


# ---------------------------------------------------------------------------
# Priority score (spec: normalization-priority.md)
# ---------------------------------------------------------------------------

def calculate_priority_score(
    task: Task,
    available_hours: float = 8.0,
    current_streak: int = 0,
    today: date | None = None,
) -> int:
    today = today or date.today()
    score = 0

    if task.deadline:
        deadline_date = task.deadline.date() if isinstance(task.deadline, datetime) else task.deadline
        days_until = (deadline_date - today).days
        if days_until <= 0:
            score += 150
        elif days_until <= 1:
            score += 100
        elif days_until <= 3:
            score += 60
        elif days_until <= 7:
            score += 30

    if task.priority == 1:
        score += 50
    elif task.priority == 2:
        score += 30
    elif task.priority == 3:
        score += 15

    if task.category == "work":
        score += 20

    if task.estimated_minutes:
        if task.estimated_minutes <= available_hours * 60:
            score += 20
        else:
            score -= 10

    if current_streak >= 5:
        score += 10
    elif current_streak >= 3:
        score += 5

    return score


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

_PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}


async def create(item: InboundItem, db: AsyncSession) -> Task:
    priority = _PRIORITY_MAP.get(item.priority_hint or "", None)
    deadline = None
    if item.deadline:
        deadline = datetime.combine(item.deadline, datetime.min.time())

    task = Task(
        title=item.extracted_title,
        origin=item.origin,
        status="pending",
        priority=priority,
        deadline=deadline,
        category=item.category,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info("Task created: {} (id={})", task.title, task.id)
    return task


async def get_pending(db: AsyncSession) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
    )
    return result.scalars().all()


def calculate_points(task: Task) -> int:
    """Pontuação ponderada por esforço (spec: melhorias.md item 8)."""
    minutes = task.estimated_minutes or 30
    if minutes < 30:
        base = 5
    elif minutes <= 60:
        base = 10
    elif minutes <= 180:
        base = 20
    else:
        base = 35
    if task.deadline and task.completed_at and task.completed_at < task.deadline:
        base = int(base * 1.5)
    return base


async def mark_done(title_fragment: str, db: AsyncSession) -> Task | None:
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.title.ilike(f"%{title_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task:
        task.status = "done"
        task.completed_at = datetime.utcnow()
        await db.commit()
        logger.info("Task done: {} (id={})", task.title, task.id)
    return task
