"""Helpers compartilhados entre os routers do dashboard."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services.time_utils import today_brt


def _today_brt() -> date:
    return today_brt()


def _now_brt() -> datetime:
    from app.services.time_utils import now_brt
    return now_brt()


def _parse_project_task(title: str, parent_map: dict | None = None, task_id: str | None = None) -> tuple[str, str]:
    """Fallback: se parent_map não tem info, tenta split por pipe."""
    if parent_map and task_id and task_id in parent_map:
        project_name, _ = parent_map[task_id]
        return project_name, title or ""
    if "|" in (title or ""):
        p, t = title.split("|", 1)
        return p.strip(), t.strip()
    return "", (title or "").strip()


async def _prefetch_parents(tasks: list, db: AsyncSession) -> dict:
    """Retorna {task_id: (project_name, deliverable_name)} para todas as tasks."""
    all_parent_ids = {t.parent_id for t in tasks if t.parent_id}
    if not all_parent_ids:
        return {}
    parents_result = await db.execute(select(Task).where(Task.id.in_(all_parent_ids)))
    parents = {str(p.id): p for p in parents_result.scalars().all()}
    grandparent_ids = {p.parent_id for p in parents.values() if p.parent_id}
    grandparents: dict = {}
    if grandparent_ids:
        gp_result = await db.execute(select(Task).where(Task.id.in_(grandparent_ids)))
        grandparents = {str(g.id): g for g in gp_result.scalars().all()}
    result = {}
    for task in tasks:
        project_name = ""
        deliverable_name = ""
        if task.parent_id:
            parent = parents.get(str(task.parent_id))
            if parent:
                if (getattr(parent, "task_type", None) or "task") == "deliverable":
                    deliverable_name = parent.title or ""
                    if parent.parent_id:
                        gp = grandparents.get(str(parent.parent_id))
                        if gp:
                            project_name = gp.title or ""
                elif (getattr(parent, "task_type", None) or "task") == "project":
                    project_name = parent.title or ""
        result[str(task.id)] = (project_name, deliverable_name)
    return result


def _serialize_deadline(deadline) -> str | None:
    """Sempre retorna ISO 8601 completo (com hora) ou None."""
    if deadline is None:
        return None
    try:
        if hasattr(deadline, "hour"):
            return deadline.isoformat()
        else:
            from datetime import time as _time
            return datetime.combine(deadline, _time(23, 59)).isoformat()
    except Exception:
        return None


def _humanize_deadline(deadline: datetime | None) -> str:
    if not deadline:
        return "sem prazo"
    today = _today_brt()
    try:
        dl_date = deadline.date() if hasattr(deadline, "date") else deadline
    except Exception:
        return "sem prazo"
    delta = (dl_date - today).days
    dias_pt = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    try:
        time_str = deadline.strftime("%Hh") if (deadline.hour or deadline.minute) else ""
    except Exception:
        time_str = ""
    suffix = f" às {time_str}" if time_str else ""
    if delta < 0:
        n = abs(delta)
        return f"⚠️ atrasada {n} dia{'s' if n > 1 else ''}"
    if delta == 0:
        return f"hoje{suffix}"
    if delta == 1:
        return f"amanhã{suffix}"
    dow = dl_date.weekday()
    date_str = dl_date.strftime("%d/%m")
    return f"{dias_pt[dow]} {date_str}{suffix}"
