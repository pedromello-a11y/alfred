"""Serviço central de criação de tasks — ÚNICA porta de entrada."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, Task
from app.services.time_utils import today_brt

logger = logging.getLogger(__name__)

VALID_TASK_TYPES = ("project", "deliverable", "task")
FINAL_STATUSES = ("done", "cancelled", "dropped")


async def create_task_unified(
    db: AsyncSession,
    *,
    title: str,
    task_type: str = "task",
    parent_id: str | None = None,
    deadline: datetime | None = None,
    deadline_type: str = "soft",
    estimated_minutes: int = 120,
    origin: str = "dashboard",
    origin_ref: str | None = None,
    category: str = "work",
    status: str = "active",
    notes_initial: str | None = None,
) -> Task:
    """Cria task padronizada. TODAS as portas de entrada devem usar esta função.

    - title: nome limpo, SEM pipes, SEM nome do projeto
    - task_type: project | deliverable | task
    - parent_id: UUID string do pai (projeto ou demanda)
    - deadline: se definido E task_type=="task", auto-agenda
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("title é obrigatório")

    if task_type not in VALID_TASK_TYPES:
        task_type = "task"

    if status in ("pending", "in_progress"):
        status = "active"

    parent_uuid = None
    if parent_id:
        try:
            parent_uuid = UUID(parent_id) if isinstance(parent_id, str) else parent_id
        except (ValueError, AttributeError):
            parent_uuid = None

    new_task = Task(
        title=title,
        task_type=task_type,
        parent_id=parent_uuid,
        deadline=deadline,
        estimated_minutes=int(estimated_minutes),
        origin=origin,
        origin_ref=origin_ref,
        status=status,
        category=category,
    )
    if hasattr(new_task, "deadline_type"):
        new_task.deadline_type = deadline_type
    if hasattr(new_task, "checklist_json"):
        new_task.checklist_json = []
    if hasattr(new_task, "notes_json"):
        notes = []
        if notes_initial:
            from app.services.time_utils import now_brt
            notes.append({
                "text": notes_initial,
                "created_at": now_brt().strftime("%d/%m %H:%M"),
            })
        new_task.notes_json = notes

    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)

    logger.info(f"Task criada: [{task_type}] '{title}' (id={new_task.id}, origin={origin})")

    if deadline and task_type == "task":
        try:
            await _auto_schedule(db, new_task)
        except Exception as e:
            logger.warning(f"Auto-agenda falhou para task {new_task.id}: {e}")

    return new_task


async def delete_task_cascade(db: AsyncSession, task: Task) -> None:
    """Marca task como cancelled e limpa AgendaBlocks."""
    from sqlalchemy import delete as sa_delete
    task.status = "cancelled"
    await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
    await db.commit()


async def complete_task_cascade(db: AsyncSession, task: Task, actual_minutes: int | None = None) -> None:
    """Marca task como done, limpa AgendaBlocks e recalcula semana."""
    from sqlalchemy import delete as sa_delete
    deadline = task.deadline  # captura antes de alterar
    task.status = "done"
    task.completed_at = datetime.now()
    if actual_minutes:
        task.actual_minutes = int(actual_minutes)
    await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
    await db.commit()

    if deadline:
        try:
            from app.services.scheduler import rebuild_week_schedule
            dl = deadline.date() if hasattr(deadline, "date") else deadline
            today = today_brt()
            ws = dl - timedelta(days=dl.weekday())
            we = ws + timedelta(days=4)
            if ws < today:
                ws = today
            await rebuild_week_schedule(db, ws, we)
        except Exception as e:
            logger.warning(f"Reschedule pós-complete falhou: {e}")


async def _auto_schedule(db: AsyncSession, task: Task) -> list[AgendaBlock]:
    """Delega ao scheduler centralizado que recalcula a semana inteira."""
    if not task.deadline:
        return []

    today = today_brt()
    try:
        dl_date = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
    except Exception:
        return []

    if dl_date < today:
        return []

    from app.services.scheduler import rebuild_week_schedule
    week_start = dl_date - timedelta(days=dl_date.weekday())
    week_end = week_start + timedelta(days=4)
    if week_start < today:
        week_start = today

    return await rebuild_week_schedule(db, week_start, week_end)


async def prefetch_parent_names(tasks: list[Task], db: AsyncSession) -> dict[str, tuple[str, str]]:
    """Retorna {task_id_str: (project_name, deliverable_name)} via parent chain.
    Resolve em 2 queries (pais + avós), sem N+1.
    """
    parent_ids = {t.parent_id for t in tasks if t.parent_id}
    if not parent_ids:
        return {}

    parents_result = await db.execute(select(Task).where(Task.id.in_(parent_ids)))
    parents = {p.id: p for p in parents_result.scalars().all()}

    grandparent_ids = {p.parent_id for p in parents.values() if p.parent_id}
    grandparents = {}
    if grandparent_ids:
        gp_result = await db.execute(select(Task).where(Task.id.in_(grandparent_ids)))
        grandparents = {g.id: g for g in gp_result.scalars().all()}

    result = {}
    for task in tasks:
        project_name = ""
        deliverable_name = ""
        if task.parent_id and task.parent_id in parents:
            parent = parents[task.parent_id]
            if parent.task_type == "deliverable":
                deliverable_name = parent.title or ""
                if parent.parent_id and parent.parent_id in grandparents:
                    project_name = grandparents[parent.parent_id].title or ""
            elif parent.task_type == "project":
                project_name = parent.title or ""
        result[str(task.id)] = (project_name, deliverable_name)

    return result
