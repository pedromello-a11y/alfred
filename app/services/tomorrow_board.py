from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock
from app.services import task_manager
from app.services.time_utils import now_brt, today_brt


def _split_title(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if "|" in text:
        project, title = text.split("|", 1)
        return project.strip(), title.strip()
    return "", text


async def build_tomorrow_board(db: AsyncSession) -> dict:
    today = today_brt()
    tomorrow = today + timedelta(days=1)
    start = datetime.combine(tomorrow, time.min)
    end = start + timedelta(days=1)

    agenda_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= start)
        .where(AgendaBlock.start_at < end)
        .order_by(AgendaBlock.start_at.asc())
    )
    agenda_blocks = list(agenda_result.scalars().all())

    active_tasks = list(await task_manager.get_active_tasks(db))
    overdue = [t for t in active_tasks if t.deadline and t.deadline.date() < today]
    due_tomorrow = [t for t in active_tasks if t.deadline and t.deadline.date() == tomorrow]
    unscheduled = [t for t in active_tasks if t not in overdue and t not in due_tomorrow][:5]

    overdue.sort(key=lambda t: (t.deadline, t.priority or 99))
    due_tomorrow.sort(key=lambda t: (t.deadline, t.priority or 99))

    priority_task = due_tomorrow[0] if due_tomorrow else (overdue[0] if overdue else (active_tasks[0] if active_tasks else None))

    suggestion = None
    if priority_task:
        project, title = _split_title(priority_task.title or "")
        suggestion = {
            "title": title or priority_task.title or "",
            "project": project,
            "reason": "prazo amanhã" if priority_task in due_tomorrow else "mais crítica em aberto",
        }

    return {
        "generatedAt": now_brt().strftime("%d/%m/%Y %H:%M BRT"),
        "agenda": [
            {
                "title": b.title,
                "start": b.start_at.strftime("%H:%M"),
                "end": b.end_at.strftime("%H:%M"),
                "type": b.block_type,
            }
            for b in agenda_blocks
        ],
        "dueTomorrow": [
            {
                "id": str(t.id),
                "title": t.title,
                "deadline": t.deadline.strftime("%d/%m %H:%M") if t.deadline else "",
                "priority": t.priority,
            }
            for t in due_tomorrow[:5]
        ],
        "overdue": [
            {
                "id": str(t.id),
                "title": t.title,
                "deadline": t.deadline.strftime("%d/%m %H:%M") if t.deadline else "",
                "priority": t.priority,
            }
            for t in overdue[:5]
        ],
        "unscheduled": [
            {
                "id": str(t.id),
                "title": t.title,
                "deadline": t.deadline.strftime("%d/%m %H:%M") if t.deadline else "sem prazo",
                "priority": t.priority,
            }
            for t in unscheduled
        ],
        "suggestion": suggestion,
    }
