"""Router do dashboard — estado, foco, amanhã e ações."""
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgendaBlock, DumpItem, PlayerStat, Streak, Task
from app.services import task_manager
from app.services.active_tasks_view import get_unified_active_view
from app.services.focus_snapshot import build_focus_snapshot
from app.services.task_manager import calculate_level, xp_progress_in_level
from app.services.text_utils import sanitize_json_strings
from app.services.tomorrow_board import build_tomorrow_board

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/state")
async def dashboard_state(
    db: AsyncSession = Depends(get_db),
    week_offset: int = 0,
) -> dict:
    focus = await build_focus_snapshot(db)
    tomorrow = await build_tomorrow_board(db)
    unified = await get_unified_active_view(db)
    week_start, week_end = _current_calendar_week_bounds()

    state = {
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
            "todayTasks": (unified.get("todayCombined") or unified.get("top3") or [])[:8],
            "alerts": [],
        },
        "horizonBoard": {
            "tomorrow": tomorrow.get("dueTomorrow", []),
            "thisWeek": unified.get("upcoming", []),
            "later": tomorrow.get("unscheduled", []),
        },
        "activeQueue": unified.get("allActive") or (unified.get("top3", []) + unified.get("rest", [])),
        "operational": {
            "nowLabel": focus.get("nowLabel", ""),
            "suggestion": focus.get("suggestion"),
            "priorityTask": (unified.get("top3") or [None])[0] or focus.get("focusTask"),
            "overdueTasks": unified.get("overdue", []),
        },
        "agendaWeekStart": week_start.isoformat(),
        "agendaWeekEnd": week_end.isoformat(),
        "agendaDeadlines": await _build_agenda_deadlines(db, week_start, week_end),
        "xp": await _build_xp_payload(db),
        _agenda_data = await _build_agenda_payload(db, week_offset)
        "agenda": _agenda_data.get("days", []),
        "agendaDeadlines": _agenda_data.get("deadlines", []),
        "agendaWeekStart": _agenda_data.get("weekStart", ""),
        "agendaWeekEnd": _agenda_data.get("weekEnd", ""),
        "dumpLibrary": await _build_dump_library(db),
        "projects": await _get_project_names(db),
    }
    return sanitize_json_strings(state)


async def _build_xp_payload(db: AsyncSession) -> dict:
    result = await db.execute(select(PlayerStat))
    all_stats = result.scalars().all()
    total_xp = sum(s.xp for s in all_stats) if all_stats else 0
    level = calculate_level(total_xp)
    current_in_level, xp_for_next = xp_progress_in_level(total_xp, level)

    streak_result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak_row = streak_result.scalar_one_or_none()
    streak = streak_row.streak_count if streak_row else 0

    percent = min(100, int(current_in_level / xp_for_next * 100)) if xp_for_next > 0 else 0
    return {"level": level, "current": total_xp, "percent": percent, "streak": streak}


def _today_brt() -> date:
    from app.services.time_utils import today_brt

    return today_brt()


def _current_calendar_week_bounds(today: date | None = None) -> tuple[date, date]:
    today = today or _today_brt()
    week_start = today - timedelta(days=today.weekday())
    return week_start, week_start + timedelta(days=6)


def _current_workweek_bounds(today: date | None = None) -> tuple[date, date]:
    week_start, _ = _current_calendar_week_bounds(today)
    return week_start, week_start + timedelta(days=4)


async def _build_agenda_payload(db: AsyncSession) -> list:
    """Returns week calendar grouped by day (0=Mon..4=Fri) for the frontend."""
    monday, friday = _current_workweek_bounds()
    monday_dt = datetime.combine(monday, datetime.min.time())
    friday_dt = datetime.combine(friday, datetime.max.time().replace(microsecond=0))

    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= monday_dt)
        .where(AgendaBlock.start_at <= friday_dt)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = result.scalars().all()

    type_map = {
        "meeting": "meeting",
        "break": "break",
        "focus": "focus",
        "personal": "personal",
        "admin": "meeting",
    }

    days: dict[int, list] = {i: [] for i in range(5)}
    for block in blocks:
        if not block.start_at:
            continue
        dow = block.start_at.weekday()
        if dow > 4:
            continue
        days[dow].append(
            {
                "title": block.title,
                "time": block.start_at.strftime("%H:%M"),
                "end": block.end_at.strftime("%H:%M") if block.end_at else "",
                "type": type_map.get(block.block_type or "focus", "focus"),
            }
        )

    return [{"day": d, "events": events} for d, events in days.items()]


async def _build_agenda_deadlines(db: AsyncSession, week_start: date, week_end: date) -> list[dict]:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.deadline.is_not(None))
        .order_by(Task.deadline.asc())
    )
    tasks = result.scalars().all()

    deadlines = []
    for task in tasks:
        if not task.deadline:
            continue

        deadline_day = task.deadline.date()
        if not (week_start <= deadline_day <= week_end):
            continue

        project = ""
        task_name = task.title or ""
        if "|" in task_name:
            project, task_name = [part.strip() for part in task_name.split("|", 1)]

        deadlines.append(
            {
                "id": str(task.id),
                "title": task.title,
                "project": project,
                "taskName": task_name,
                "date": deadline_day.isoformat(),
                "label": task.deadline.strftime("%d/%m %H:%M"),
                "day": deadline_day.weekday(),
                "priority": task.priority,
            }
        )

    return deadlines


async def _build_dump_library(db: AsyncSession) -> dict:
    """Returns dump library grouped by category with needs-review and recent items."""
    from sqlalchemy import func

    cat_result = await db.execute(
        select(DumpItem.category, func.count(DumpItem.id))
        .where(DumpItem.status != "archived")
        .group_by(DumpItem.category)
    )
    categories = [{"name": cat or "outros", "count": cnt} for cat, cnt in cat_result.all()]

    review_result = await db.execute(
        select(DumpItem)
        .where(DumpItem.status.in_(("unknown", "categorized")))
        .where((DumpItem.confidence == None) | (DumpItem.confidence < 0.5))
        .order_by(DumpItem.created_at.desc())
        .limit(10)
    )
    needs_review = [_dump_to_dict(d) for d in review_result.scalars().all()]

    recent_result = await db.execute(
        select(DumpItem)
        .where(DumpItem.status != "archived")
        .order_by(DumpItem.created_at.desc())
        .limit(20)
    )
    recent = [_dump_to_dict(d) for d in recent_result.scalars().all()]

    return {"categories": categories, "needsReview": needs_review, "recent": recent}


def _dump_to_dict(d: DumpItem) -> dict:
    return {
        "id": str(d.id),
        "title": d.rewritten_title or d.raw_text[:80] or "",
        "category": d.category or "outros",
        "confidence": d.confidence,
        "status": d.status,
        "rawText": d.raw_text,
        "summary": d.summary or "",
    }


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


@router.post("/sync-gcal")
async def sync_gcal(db: AsyncSession = Depends(get_db)) -> dict:
    """Triggers a manual Google Calendar sync and cleans up junk agenda blocks."""
    junk_result = await db.execute(
        select(AgendaBlock).where(
            (AgendaBlock.source != "gcal") | (AgendaBlock.source == None)
        )
    )
    junk_blocks = junk_result.scalars().all()
    junk_to_delete = [b for b in junk_blocks if b.title and len(b.title) > 60]
    for b in junk_to_delete:
        await db.delete(b)
    await db.commit()

    from app.services import gcal_client

    if not gcal_client._is_configured():
        return {"status": "error", "message": "gcal not configured", "deleted_junk": len(junk_to_delete)}

    synced = await gcal_client.sync_to_agenda_blocks(db)
    return {"status": "ok", "synced": synced, "deleted_junk": len(junk_to_delete)}
