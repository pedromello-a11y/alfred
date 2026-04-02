from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, DailyPlan
from app.services import task_manager
from app.services.time_utils import now_brt, now_brt_naive, today_brt


def _split_title(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    if "|" in text:
        project, title = text.split("|", 1)
        return project.strip(), title.strip()
    return "", text


async def build_focus_snapshot(db: AsyncSession) -> dict:
    now = now_brt()
    now_naive = now_brt_naive()
    today = today_brt()
    end_today = datetime.combine(today + timedelta(days=1), time.min)

    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at < end_today)
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = list(result.scalars().all())

    current_block = None
    next_block = None
    for block in blocks:
        if block.start_at <= now_naive < block.end_at:
            current_block = block
        elif block.start_at > now_naive and next_block is None:
            next_block = block

    active = list(await task_manager.get_active_tasks(db))
    due_today = [t for t in active if t.deadline and t.deadline.date() == today]
    overdue = [t for t in active if t.deadline and t.deadline.date() < today]

    plan_result = await db.execute(select(DailyPlan).where(DailyPlan.plan_date == today))
    plan = plan_result.scalar_one_or_none()
    planned_ids = set()
    if plan and plan.tasks_planned and isinstance(plan.tasks_planned, dict):
        planned_ids = set(plan.tasks_planned.get("ids", []))

    planned_today = [t for t in active if str(t.id) in planned_ids and t not in due_today and t not in overdue]
    today_combined = due_today + planned_today

    due_today.sort(key=lambda t: (t.deadline, t.priority or 99))
    overdue.sort(key=lambda t: (t.deadline, t.priority or 99))
    planned_today.sort(key=lambda t: (t.priority or 99, t.created_at or datetime.max))

    focus_task = due_today[0] if due_today else (planned_today[0] if planned_today else (overdue[0] if overdue else (active[0] if active else None)))
    next_task = active[1] if len(active) > 1 else None

    suggestion = None
    if current_block:
        suggestion = {"title": current_block.title, "reason": f"bloco atual até {current_block.end_at.strftime('%H:%M')}"}
    elif focus_task:
        suggestion = {"title": focus_task.title, "reason": "task mais urgente ativa"}
    elif next_block:
        suggestion = {"title": next_block.title, "reason": f"próximo bloco às {next_block.start_at.strftime('%H:%M')}"}
    else:
        suggestion = {"title": "nenhum foco ativo", "reason": "sem blocos e sem tasks"}

    def task_payload(task):
        if not task:
            return None
        project, title = _split_title(task.title or "")
        return {
            "id": str(task.id),
            "title": task.title,
            "project": project,
            "taskName": title,
            "deadline": task.deadline.strftime('%d/%m %H:%M') if task.deadline else 'sem prazo',
            "priority": task.priority,
        }

    def block_payload(block):
        if not block:
            return None
        return {
            "title": block.title,
            "start": block.start_at.strftime('%H:%M'),
            "end": block.end_at.strftime('%H:%M'),
            "type": block.block_type,
        }

    return {
        "nowLabel": now.strftime('%H:%M BRT'),
        "currentBlock": block_payload(current_block),
        "nextBlock": block_payload(next_block),
        "focusTask": task_payload(focus_task),
        "nextTask": task_payload(next_task),
        "suggestion": suggestion,
        "dueToday": [task_payload(t) for t in today_combined[:5]],
        "overdue": [task_payload(t) for t in overdue[:5]],
        "active": [task_payload(t) for t in active[:12]],
    }
