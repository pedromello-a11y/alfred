"""View unificada de tasks ativas — fonte única de verdade para dashboard e WhatsApp."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyPlan, Task
from app.services import task_manager
from app.services.time_utils import today_brt


def _status_label(status: str) -> str:
    return {
        "done": "concluída",
        "in_progress": "em andamento",
        "pending": "pendente",
        "delegated": "delegada",
        "dropped": "removida",
    }.get(status, status)


def _make_badge(task: Task) -> str:
    if task.is_boss_fight:
        return "⚔️ boss"
    if task.priority and task.priority <= 2:
        return "alta"
    if task.deadline:
        today = today_brt()
        if task.deadline.date() < today:
            return "atrasada"
        if task.deadline.date() == today:
            return "hoje"
    return ""


def _task_to_dict(task: Task) -> dict:
    project = ""
    task_name = task.title or ""
    if "|" in task_name:
        parts = task_name.split("|", 1)
        project = parts[0].strip()
        task_name = parts[1].strip()

    priority_labels = {1: "hi", 2: "hi", 3: "md", 4: "lo", 5: "lo"}

    return {
        "id": str(task.id),
        "title": task.title,
        "project": project,
        "taskName": task_name,
        "name": task.title,
        "status": task.status,
        "statusLabel": _status_label(task.status),
        "priority": priority_labels.get(task.priority, "md"),
        "priorityLabel": f"p{task.priority}" if task.priority else "",
        "deadline": task.deadline.strftime("%d/%m %H:%M") if task.deadline else "sem prazo",
        "deadlineRaw": task.deadline.isoformat() if task.deadline else None,
        "rawDate": task.deadline.isoformat() if task.deadline else "",
        "estimatedMinutes": task.estimated_minutes,
        "estimate": f"~{task.estimated_minutes}min" if task.estimated_minutes else "",
        "category": task.category,
        "isBossFight": task.is_boss_fight,
        "timesPlanned": task.times_planned,
        "badge": _make_badge(task),
        "bdgcls": "bdg-a" if (task.priority or 99) <= 2 else "bdg-m",
        "dot": priority_labels.get(task.priority, "md"),
        "cls": "",
    }


async def get_unified_active_view(db: AsyncSession) -> dict:
    """Retorna a view unificada de tasks ativas.

    Regras:
    - nunca inclui tasks category system/backlog
    - nunca inclui títulos detectados como system
    - usa a mesma ordenação para todos os canais
    """
    today = today_brt()
    active = list(await task_manager.get_active_tasks(db, include_system=False))
    recent_done = list(await task_manager.get_recently_done(db, limit=5, include_system=False))

    plan_result = await db.execute(select(DailyPlan).where(DailyPlan.plan_date == today))
    plan = plan_result.scalar_one_or_none()
    planned_ids = set()
    if plan and plan.tasks_planned and isinstance(plan.tasks_planned, dict):
        planned_ids = set(plan.tasks_planned.get("ids", []))

    overdue = [t for t in active if t.deadline and t.deadline.date() < today]
    due_today = [t for t in active if t.deadline and t.deadline.date() == today]
    planned_today = [
        t for t in active
        if str(t.id) in planned_ids and t not in due_today and t not in overdue
    ]
    upcoming = [t for t in active if t.deadline and t.deadline.date() > today]
    no_deadline = [t for t in active if not t.deadline and t not in planned_today]

    overdue.sort(key=lambda t: (t.deadline, t.priority or 99))
    due_today.sort(key=lambda t: (t.deadline, t.priority or 99))
    planned_today.sort(key=lambda t: (t.priority or 99, t.created_at or datetime.min))
    upcoming.sort(key=lambda t: (t.deadline, t.priority or 99))
    no_deadline.sort(key=lambda t: (t.priority or 99, t.created_at or datetime.min))

    today_combined = overdue + due_today + planned_today
    all_sorted = overdue + due_today + planned_today + upcoming + no_deadline

    seen_ids = set()
    all_unique = []
    for task in all_sorted:
        if task.id in seen_ids:
            continue
        seen_ids.add(task.id)
        all_unique.append(task)

    top3 = all_unique[:3]
    rest = all_unique[3:]

    return {
        "total": len(active),
        "top3": [_task_to_dict(t) for t in top3],
        "todayCombined": [_task_to_dict(t) for t in today_combined],
        "overdue": [_task_to_dict(t) for t in overdue],
        "dueToday": [_task_to_dict(t) for t in due_today],
        "plannedToday": [_task_to_dict(t) for t in planned_today],
        "upcoming": [_task_to_dict(t) for t in upcoming[:5]],
        "noDeadline": [_task_to_dict(t) for t in no_deadline[:5]],
        "recentDone": [_task_to_dict(t) for t in recent_done],
        "rest": [_task_to_dict(t) for t in rest[:10]],
        "allActive": [_task_to_dict(t) for t in all_unique[:12]],
    }


def format_active_tasks_for_whatsapp(view: dict) -> str:
    total = view["total"]
    if total == 0:
        return "Você não tem tarefas ativas agora."

    lines = [f"Ativas agora ({total}):"]

    for i, task in enumerate(view["top3"], 1):
        extra = []
        if task["estimate"]:
            extra.append(task["estimate"])
        if task["priorityLabel"]:
            extra.append(task["priorityLabel"])
        deadline = task.get("deadline", "sem prazo")
        if deadline != "sem prazo":
            extra.append(f"prazo {deadline}")
        if task["isBossFight"]:
            extra.append("⚔️ boss")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{i}. *{task['title']}* — {task['statusLabel']}{suffix}")

    if view["rest"]:
        lines.append("\nEm acompanhamento:")
        for task in view["rest"][:4]:
            lines.append(f"- {task['title']} — {task['statusLabel']}")

    if view["overdue"]:
        lines.append(f"\n⚠️ {len(view['overdue'])} tarefa(s) atrasada(s)")

    if view["recentDone"]:
        lines.append("\nResolvidas por último:")
        for task in view["recentDone"][:3]:
            lines.append(f"- {task['title']}")

    lines.append("\nQual dessas está na sua mão agora?")
    return "\n".join(lines)
