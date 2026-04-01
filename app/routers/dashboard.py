"""Router do dashboard — estado, foco, amanhã e ações."""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PlayerStat, Streak, Task
from app.services import task_manager
from app.services.focus_snapshot import build_focus_snapshot
from app.services.tomorrow_board import build_tomorrow_board

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ── Leitura ──────────────────────────────────────────────────────────────

@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)) -> dict:
    focus = await build_focus_snapshot(db)
    tomorrow = await build_tomorrow_board(db)
    return {
        "focus": {
            "title": (focus.get("focusTask") or {}).get("title", "nenhuma tarefa ativa"),
        },
        "next": {
            "title": (focus.get("nextTask") or {}).get("title", "—"),
            "note": "",
        },
        "focusBoard": {
            "currentBlock": focus.get("currentBlock"),
            "nextBlock": focus.get("nextBlock"),
            "todayTasks": focus.get("dueToday", []),
            "alerts": [],
        },
        "horizonBoard": {
            "tomorrow": tomorrow.get("dueTomorrow", []),
            "thisWeek": [],
            "later": tomorrow.get("unscheduled", []),
        },
        "activeQueue": focus.get("active", []),
        "operational": {
            "nowLabel": focus.get("nowLabel", ""),
            "suggestion": focus.get("suggestion"),
            "priorityTask": focus.get("focusTask"),
            "overdueTasks": focus.get("overdue", []),
        },
        "dumpLibrary": {
            "categories": [],
            "needsReview": [],
            "recent": [],
        },
        "xp": await _build_xp_payload(db),
        "agenda": [],
        "projects": await _get_project_names(db),
    }


async def _build_xp_payload(db: AsyncSession) -> dict:
    result = await db.execute(select(PlayerStat).order_by(PlayerStat.xp.desc()).limit(1))
    top = result.scalar_one_or_none()
    total_xp = top.xp if top else 0
    level = top.level if top else 1

    streak_result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak_row = streak_result.scalar_one_or_none()
    streak = streak_row.streak_count if streak_row else 0

    xp_for_next = (level + 1) * 100
    percent = min(100, int((total_xp % 100) / 100 * 100)) if xp_for_next > 0 else 0

    return {"level": level, "current": total_xp, "percent": percent, "streak": streak}


async def _get_project_names(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Task.title).where(Task.status.in_(("pending", "in_progress")))
    )
    titles = result.scalars().all()
    projects = set()
    for title in titles:
        if "|" in (title or ""):
            project = title.split("|", 1)[0].strip()
            if project:
                projects.add(project)
    return sorted(projects)


@router.get("/focus")
async def dashboard_focus(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_focus_snapshot(db)


@router.get("/tomorrow")
async def dashboard_tomorrow(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_tomorrow_board(db)


# ── Ações ────────────────────────────────────────────────────────────────

class ActionPayload(BaseModel):
    task_id: str
    action: str
    note: str | None = None
    date: str | None = None


@router.post("/action")
async def dashboard_action(
    payload: ActionPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task_uuid = UUID(payload.task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}

    action = (payload.action or "").lower().strip()

    if action in ("concluida", "concluída", "done"):
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc)
        if payload.note:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            entry = f"[{ts}] {payload.note.strip()}"
            task.notes = f"{task.notes}\n{entry}" if task.notes else entry
        await db.commit()
        return {"status": "ok", "action": "done", "title": task.title}

    if action in ("excluir", "delete", "remover"):
        task.status = "cancelled"
        await db.commit()
        return {"status": "ok", "action": "cancelled", "title": task.title}

    if action in ("adiar", "postpone") and payload.date:
        try:
            task.deadline = datetime.fromisoformat(payload.date)
        except ValueError:
            pass
        await db.commit()
        return {"status": "ok", "action": "postponed", "title": task.title}

    return {"status": "error", "message": f"unknown action: {action}"}


class CreateTaskPayload(BaseModel):
    title: str
    project: str | None = None
    date: str | None = None
    priority: int | None = None


@router.post("/create-task")
async def dashboard_create_task(
    payload: CreateTaskPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    title = (payload.title or "").strip()
    if not title:
        return {"status": "error", "message": "title is required"}

    project = (payload.project or "").strip()
    full_title = f"{project} | {title}" if project else title
    full_title = task_manager.canonicalize_task_title(full_title)

    deadline = None
    if payload.date:
        try:
            deadline = datetime.fromisoformat(payload.date)
        except ValueError:
            pass

    new_task = Task(
        title=full_title,
        origin="dashboard",
        status="pending",
        priority=payload.priority,
        deadline=deadline,
        category="work",
    )
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)

    return {"status": "ok", "id": str(new_task.id), "title": new_task.title}


class TaskEditPayload(BaseModel):
    task_id: str
    title: str | None = None
    project: str | None = None
    date: str | None = None
    note: str | None = None


@router.post("/task-edit")
async def dashboard_task_edit(
    payload: TaskEditPayload,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        task_uuid = UUID(payload.task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}

    title = (payload.title or "").strip()
    project = (payload.project or "").strip()
    if title:
        full_title = f"{project} | {title}" if project else title
        task.title = task_manager.canonicalize_task_title(full_title)

    if payload.date:
        try:
            task.deadline = datetime.fromisoformat(payload.date)
        except ValueError:
            pass

    if payload.note:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"[{ts}] {payload.note.strip()}"
        task.notes = f"{task.notes}\n{entry}" if task.notes else entry

    await db.commit()
    return {"status": "ok", "title": task.title}
