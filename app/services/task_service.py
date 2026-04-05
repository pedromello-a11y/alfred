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
    """Marca task como done e limpa AgendaBlocks."""
    from sqlalchemy import delete as sa_delete
    task.status = "done"
    task.completed_at = datetime.now()
    if actual_minutes:
        task.actual_minutes = int(actual_minutes)
    await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
    await db.commit()


async def _auto_schedule(db: AsyncSession, task: Task) -> list[AgendaBlock]:
    """Cria blocos na agenda para a task, respeitando buffer e eventos existentes."""
    if not task.deadline:
        return []

    today = today_brt()
    try:
        dl_date = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
    except Exception:
        return []

    if dl_date < today:
        return []

    week_start = dl_date - timedelta(days=dl_date.weekday())
    week_end = week_start + timedelta(days=4)

    if week_start < today:
        week_start = today

    estimate = task.estimated_minutes or 120
    WORK_START = 8 * 60
    WORK_END = 20 * 60
    MAX_BLOCK = 120
    MIN_BLOCK = 15

    from datetime import time as dt_time
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time())

    existing_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= week_start_dt)
        .where(AgendaBlock.start_at < week_end_dt)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    existing = existing_result.scalars().all()

    occupied: dict = {}
    for block in existing:
        if not block.start_at or not block.end_at:
            continue
        d = block.start_at.date()
        s_min = block.start_at.hour * 60 + block.start_at.minute
        e_min = block.end_at.hour * 60 + block.end_at.minute
        occupied.setdefault(d, []).append((s_min, e_min))

    def _calc_buffer(available: int, allocated: int) -> int:
        if available <= 0:
            return 0
        load = allocated / available
        if load <= 0.5:
            return 120
        elif load <= 0.75:
            return 60
        else:
            return 30

    def _free_slots(day, max_to_allocate: int) -> list[tuple[int, int]]:
        if day.weekday() >= 5 or day < today:
            return []

        total_available = WORK_END - WORK_START
        busy = sorted(occupied.get(day, []), key=lambda x: x[0])
        already_allocated = sum(max(0, e - s) for s, e in busy)

        buffer = _calc_buffer(total_available, already_allocated)
        max_allocatable = max(0, total_available - already_allocated - buffer)

        if max_allocatable < MIN_BLOCK:
            return []

        max_allocatable = min(max_allocatable, max_to_allocate)

        free = []
        cursor = WORK_START
        alloc_so_far = 0

        for (bs, be) in busy:
            if cursor < bs and alloc_so_far < max_allocatable:
                gap = bs - cursor
                usable = min(gap, max_allocatable - alloc_so_far)
                if usable >= MIN_BLOCK:
                    free.append((cursor, cursor + usable))
                    alloc_so_far += usable
            cursor = max(cursor, be)

        if cursor < WORK_END and alloc_so_far < max_allocatable:
            gap = WORK_END - cursor
            usable = min(gap, max_allocatable - alloc_so_far)
            if usable >= MIN_BLOCK:
                free.append((cursor, cursor + usable))

        return free

    remaining = estimate
    created = []
    current_day = week_start

    while current_day <= week_end and remaining > 0:
        free = _free_slots(current_day, remaining)

        for (slot_start, slot_end) in free:
            if remaining <= 0:
                break

            available = slot_end - slot_start

            if remaining <= available:
                duration = remaining
            else:
                duration = min(available, MAX_BLOCK)

            if duration < MIN_BLOCK:
                continue

            start_dt = datetime.combine(current_day, datetime.min.time()) + timedelta(minutes=slot_start)
            end_dt = start_dt + timedelta(minutes=duration)

            new_block = AgendaBlock(
                title=task.title or "",
                start_at=start_dt,
                end_at=end_dt,
                block_type="suggested",
                source="alfred",
                status="planned",
                task_id=task.id,
                pinned=False,
            )
            db.add(new_block)
            created.append(new_block)

            occupied.setdefault(current_day, []).append((slot_start, slot_start + duration))
            remaining -= duration

        current_day += timedelta(days=1)

    if created:
        await db.commit()
        logger.info(f"Auto-agenda: {len(created)} blocos para task '{task.title}' ({estimate}min)")

    return created


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
