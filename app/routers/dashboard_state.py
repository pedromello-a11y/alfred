"""Dashboard state — GET endpoints de leitura."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ACTIVE_STATUSES
from app.database import get_db
from app.models import AgendaBlock, DumpItem, PlayerStat, Streak, Task
from app.services.block_engine import find_next_block_for_task
from app.services.dashboard_helpers import (
    _humanize_deadline,
    _now_brt,
    _parse_project_task,
    _prefetch_parents,
    _serialize_deadline,
    _today_brt,
)
from app.services.focus_snapshot import build_focus_snapshot
from app.services.gamification_service import calculate_level, xp_progress_in_level
from app.services.text_utils import sanitize_json_strings
from app.services.tomorrow_board import build_tomorrow_board

logger = logging.getLogger("alfred")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _get_task_group(task: Task, today: date) -> str:
    if not task.deadline:
        return "noPrazo"
    try:
        dl_date = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        delta = (dl_date - today).days
    except Exception:
        return "noPrazo"
    if delta < 0:
        return "overdue"
    if delta <= 6:
        return "thisWeek"
    if delta <= 13:
        return "nextWeek"
    return "noPrazo"


def _task_to_queue_item(task: Task, today: date, parent_map: dict | None = None) -> dict:
    project, _ = _parse_project_task(task.title, parent_map, str(task.id))
    if parent_map and str(task.id) in parent_map:
        project = parent_map[str(task.id)][0]
    task_name = task.title or ""
    dl_type = getattr(task, "deadline_type", None) or "soft"
    checklist = getattr(task, "checklist_json", None) or []
    return {
        "id": str(task.id),
        "project": project,
        "taskName": task_name,
        "fullTitle": task.title,
        "deadline": _serialize_deadline(task.deadline),
        "deadlineRaw": _serialize_deadline(task.deadline),
        "deadlineHuman": _humanize_deadline(task.deadline),
        "deadlineType": dl_type,
        "status": task.status,
        "taskType": task.task_type or "task",
        "category": getattr(task, "category", "work") or "work",
        "estimate": task.estimated_minutes or 120,
        "group": _get_task_group(task, today),
        "checklistTotal": len(checklist),
        "checklistDone": sum(1 for i in checklist if i.get("done")),
        "blocked": getattr(task, 'blocked', False) or False,
        "blocked_reason": getattr(task, 'blocked_reason', None) or "",
        "blocked_until": task.blocked_until.isoformat() if getattr(task, 'blocked_until', None) else None,
    }


async def _build_focus_v3(db: AsyncSession) -> tuple[dict, dict | None]:
    today = _today_brt()
    now_brt_dt = _now_brt()
    now_naive = now_brt_dt.replace(tzinfo=None) if now_brt_dt.tzinfo else now_brt_dt

    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress", "active")))
        .where(Task.category != "personal")
        .order_by(
            Task.status.desc(),
            Task.deadline.asc().nulls_last(),
            Task.priority.asc().nulls_last(),
        )
        .limit(10)
    )
    tasks = result.scalars().all()
    work_tasks = [t for t in tasks if not (t.category or "").startswith("personal")]
    if not work_tasks:
        return {"status": "empty"}, None

    focus_task = work_tasks[0]
    project, task_name = _parse_project_task(focus_task.title)
    dl_type = getattr(focus_task, "deadline_type", None) or "soft"

    block_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at <= now_naive)
        .where(AgendaBlock.end_at >= now_naive)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
        .limit(1)
    )
    current_block = block_result.scalar_one_or_none()

    block_payload = None
    if current_block and current_block.start_at and current_block.end_at:
        elapsed = int((now_naive - current_block.start_at).total_seconds() / 60)
        remaining = int((current_block.end_at - now_naive).total_seconds() / 60)
        block_payload = {
            "start": current_block.start_at.strftime("%H:%M"),
            "end": current_block.end_at.strftime("%H:%M"),
            "blockNumber": 1,
            "totalBlocks": 1,
            "elapsedMinutes": max(0, elapsed),
            "remainingMinutes": max(0, remaining),
        }

    has_deadline_today = False
    if focus_task.deadline:
        try:
            dl_date = focus_task.deadline.date() if hasattr(focus_task.deadline, "date") else focus_task.deadline
            has_deadline_today = dl_date == today
        except (TypeError, AttributeError) as e:
            logger.warning("Erro ao comparar deadline de focus_task %s: %s", focus_task.id, e)

    if has_deadline_today:
        status = "urgent"
    elif current_block:
        status = "active"
    else:
        status = "free"

    checklist = getattr(focus_task, "checklist_json", None) or []
    focus = {
        "taskId": str(focus_task.id),
        "project": project,
        "taskName": task_name,
        "fullTitle": focus_task.title,
        "deadline": _serialize_deadline(focus_task.deadline),
        "deadlineHuman": _humanize_deadline(focus_task.deadline),
        "deadlineType": dl_type,
        "estimate": focus_task.estimated_minutes or 120,
        "currentBlock": block_payload,
        "status": status,
        "checklistTotal": len(checklist),
        "checklistDone": sum(1 for i in checklist if i.get("done")),
    }

    next_task_obj = work_tasks[1] if len(work_tasks) > 1 else None
    next_payload = None
    if next_task_obj:
        np, nt = _parse_project_task(next_task_obj.title)
        next_payload = {
            "taskId": str(next_task_obj.id),
            "project": np,
            "taskName": nt,
            "deadline": _serialize_deadline(next_task_obj.deadline),
            "deadlineHuman": _humanize_deadline(next_task_obj.deadline),
            "startTime": "",
            "deadlineType": getattr(next_task_obj, "deadline_type", None) or "soft",
        }

    return focus, next_payload


async def _build_today_tasks(db: AsyncSession) -> list[dict]:
    today = _today_brt()
    tomorrow = today + timedelta(days=1)
    now_dt = _now_brt().replace(tzinfo=None) if _now_brt().tzinfo else _now_brt()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(tomorrow, datetime.min.time())

    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress", "active", "done", "completed")))
        .where(Task.deadline >= today_start)
        .where(Task.deadline < today_end)
        .order_by(Task.deadline.asc())
    )
    tasks = result.scalars().all()

    blocks_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= today_start)
        .where(AgendaBlock.start_at < today_end)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = blocks_result.scalars().all()

    task_block_map: dict[str, AgendaBlock] = {}
    for b in blocks:
        if b.task_id:
            task_block_map[str(b.task_id)] = b

    end_of_day = datetime.combine(today, datetime.min.time().replace(hour=20))
    busy_mins = 0
    for b in blocks:
        if not b.start_at or not b.end_at:
            continue
        b_start = max(b.start_at, now_dt)
        b_end = min(b.end_at, end_of_day)
        if b_end > b_start:
            busy_mins += int((b_end - b_start).total_seconds() / 60)
    total_remaining_mins = max(0, int((end_of_day - now_dt).total_seconds() / 60))
    available_hours = round((total_remaining_mins - busy_mins) / 60, 1)

    items = []
    for task in tasks:
        project, task_name = _parse_project_task(task.title)
        checklist = getattr(task, "checklist_json", None) or []
        blk = task_block_map.get(str(task.id))
        scheduled_start = blk.start_at.strftime("%H:%M") if blk and blk.start_at else None
        scheduled_end = blk.end_at.strftime("%H:%M") if blk and blk.end_at else None
        items.append({
            "id": str(task.id),
            "project": project,
            "taskName": task_name,
            "deadline": _serialize_deadline(task.deadline),
            "deadlineHuman": _humanize_deadline(task.deadline),
            "deadlineType": getattr(task, "deadline_type", None) or "soft",
            "status": task.status or "pending",
            "jira_key": (task.origin_ref or "") if task.origin == "jira" else "",
            "scheduledStart": scheduled_start,
            "scheduledEnd": scheduled_end,
            "estimatedMinutes": task.estimated_minutes or 0,
            "checklistTotal": len(checklist),
            "checklistDone": sum(1 for i in checklist if i.get("done")),
            "availableHours": available_hours,
        })

    def sort_key(it):
        if it["scheduledStart"]:
            return (0, it["scheduledStart"])
        return (1, it["deadline"] or "9999")

    items.sort(key=sort_key)
    return items


async def _build_active_queue(db: AsyncSession) -> list[dict]:
    today = _today_brt()
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress", "active")))
        .where(Task.task_type.notin_(["project", "deliverable"]))
        .order_by(Task.deadline.asc().nulls_last(), Task.priority.asc().nulls_last())
        .limit(50)
    )
    tasks = result.scalars().all()
    parent_map = await _prefetch_parents(tasks, db)
    return [_task_to_queue_item(t, today, parent_map) for t in tasks]


def _current_workweek_bounds(ref: date | None = None) -> tuple[date, date]:
    today = ref or _today_brt()
    if today.weekday() == 6:
        today = today + timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


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
        try:
            deadline_day = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        except Exception:
            continue
        if not (week_start <= deadline_day <= week_end):
            continue
        project, task_name = _parse_project_task(task.title)
        deadlines.append({
            "id": str(task.id),
            "title": task.title,
            "project": project,
            "taskName": task_name,
            "date": deadline_day.isoformat(),
            "label": task.deadline.strftime("%d/%m %H:%M"),
            "day": deadline_day.weekday(),
            "priority": task.priority,
            "deadlineType": getattr(task, "deadline_type", None) or "soft",
        })
    return deadlines


async def _build_agenda_payload(db: AsyncSession, week_offset: int = 0) -> dict:
    today = _today_brt() + timedelta(weeks=week_offset)
    monday, friday = _current_workweek_bounds(today)
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

    type_map = {"meeting": "meeting", "break": "break", "focus": "focus", "personal": "personal", "admin": "meeting"}
    days: dict[int, list] = {i: [] for i in range(5)}
    alfred_blocks: list[dict] = []

    for block in blocks:
        if not block.start_at:
            continue
        dow = block.start_at.weekday()
        if dow > 4:
            continue
        source = block.source or "manual"
        btype = block.block_type or "focus"

        if source in ("alfred", "system") or btype in ("suggested", "quick"):
            project, task_name = _parse_project_task(block.title)
            alfred_blocks.append({
                "blockId": str(block.id),
                "taskId": str(block.task_id) if block.task_id else None,
                "day": dow,
                "title": task_name or block.title,
                "shortTitle": (task_name or block.title or "")[:30],
                "fullTitle": block.title,
                "project": project,
                "start": block.start_at.strftime("%H:%M"),
                "time": block.start_at.strftime("%H:%M"),
                "end": block.end_at.strftime("%H:%M") if block.end_at else "",
                "type": "quick" if btype == "quick" else "suggested",
                "source": "alfred",
                "pinned": bool(getattr(block, "pinned", False)),
                "part": getattr(block, "part", None),
            })
        else:
            days[dow].append({
                "title": block.title,
                "start": block.start_at.strftime("%H:%M"),
                "time": block.start_at.strftime("%H:%M"),
                "end": block.end_at.strftime("%H:%M") if block.end_at else "",
                "type": type_map.get(btype, "focus"),
                "source": source,
            })

    deadlines = await _build_agenda_deadlines(db, monday, friday)

    suggested = alfred_blocks
    risk_alert = None

    return {
        "days": [
            {"day": d, "date": (monday + timedelta(days=d)).isoformat(), "blocks": events, "events": events}
            for d, events in days.items()
        ],
        "suggestedBlocks": suggested,
        "pauses": [],
        "deadlines": deadlines,
        "weekStart": monday.isoformat(),
        "weekEnd": friday.isoformat(),
        "_riskAlert": risk_alert,
        "_monday": monday,
        "_friday": friday,
    }


async def _build_dump_library(db: AsyncSession) -> dict:
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
        select(DumpItem).where(DumpItem.status != "archived").order_by(DumpItem.created_at.desc()).limit(20)
    )
    recent = [_dump_to_dict(d) for d in recent_result.scalars().all()]
    return {"categories": categories, "needsReview": needs_review, "recent": recent}


def _dump_to_dict(d: DumpItem) -> dict:
    return {
        "id": str(d.id),
        "title": d.rewritten_title or (d.raw_text[:80] if d.raw_text else ""),
        "category": d.category or "outros",
        "confidence": d.confidence,
        "status": d.status,
        "rawText": d.raw_text,
        "summary": d.summary or "",
        "createdAt": d.created_at.strftime("%d/%m") if d.created_at else "",
    }


async def _get_project_names(db: AsyncSession) -> list[str]:
    result = await db.execute(
        select(Task.title).where(Task.status.in_(("pending", "in_progress")))
    )
    titles = result.scalars().all()
    projects: set[str] = set()
    for title in titles:
        if "|" in (title or ""):
            p = title.split("|", 1)[0].strip()
            if p:
                projects.add(p)
    return sorted(projects)


async def _build_xp_payload(db: AsyncSession) -> dict:
    result = await db.execute(select(PlayerStat))
    all_stats = result.scalars().all()
    total_xp = sum(s.xp for s in all_stats) if all_stats else 0
    level = calculate_level(total_xp)
    current_in_level, xp_for_next = xp_progress_in_level(total_xp, level)
    streak_result = await db.execute(select(Streak).order_by(Streak.streak_date.desc()).limit(1))
    streak_row = streak_result.scalar_one_or_none()
    streak = streak_row.streak_count if streak_row else 0
    percent = min(100, int(current_in_level / xp_for_next * 100)) if xp_for_next > 0 else 0
    return {"level": level, "current": total_xp, "percent": percent, "streak": streak}


def _calc_deficit(active_queue: list[dict], agenda_data: dict, week_offset: int) -> dict:
    from app.services.time_utils import today_brt as _tb
    today = _tb() + timedelta(weeks=week_offset)
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)

    total_needed = 0
    movable: list[dict] = []
    for item in active_queue:
        dl = item.get("deadline")
        if not dl:
            continue
        try:
            dl_date = datetime.fromisoformat(dl).date()
        except Exception:
            continue
        if dl_date <= friday:
            est = item.get("estimate", 120)
            total_needed += est
            if item.get("deadlineType") != "hard":
                movable.append(item)

    suggested = agenda_data.get("suggestedBlocks", [])
    total_available = 0
    for b in suggested:
        try:
            s = datetime.strptime(b["start"], "%H:%M")
            e = datetime.strptime(b["end"], "%H:%M")
            total_available += int((e - s).total_seconds() / 60)
        except (ValueError, KeyError) as err:
            logger.warning("Bloco com horário inválido ignorado: %s → %s", b, err)
    if total_available == 0:
        total_available = 5 * 10 * 60

    needed_h = round(total_needed / 60, 1)
    available_h = round(total_available / 60, 1)
    overflow_h = round(max(0, needed_h - available_h), 1)

    movable.sort(key=lambda i: i.get("deadline") or "9999", reverse=True)

    return {
        "totalNeeded": needed_h,
        "totalAvailable": available_h,
        "overflow": overflow_h,
        "movableTasks": movable[:5],
    }


async def _build_personal_suggestion(db: AsyncSession) -> dict | None:
    result = await db.execute(
        select(Task)
        .where(Task.category == "personal")
        .where(Task.status.in_(("pending", "in_progress", "active")))
        .order_by(Task.priority.asc().nulls_last(), Task.created_at.asc())
        .limit(5)
    )
    tasks = result.scalars().all()
    if not tasks:
        return None
    t = tasks[0]
    _, task_name = _parse_project_task(t.title)
    return {
        "id": str(t.id),
        "title": task_name or t.title,
        "estimatedMinutes": t.estimated_minutes or 30,
    }


def _task_to_flat(t: Task, parent_map: dict | None = None) -> dict:
    project = parent_map[str(t.id)][0] if (parent_map and str(t.id) in parent_map) else _parse_project_task(t.title)[0]
    task_name = t.title or ""
    checklist = getattr(t, "checklist_json", None) or []
    origin_ref = getattr(t, "origin_ref", None) or ""
    jira_key = origin_ref if (getattr(t, "origin", "") == "jira") else ""
    return {
        "id": str(t.id),
        "title": t.title,
        "taskName": task_name,
        "project": project,
        "parent_id": str(t.parent_id) if t.parent_id else None,
        "task_type": getattr(t, "task_type", "task") or "task",
        "status": t.status,
        "deadline": _serialize_deadline(t.deadline),
        "deadlineHuman": _humanize_deadline(t.deadline),
        "estimated_minutes": t.estimated_minutes,
        "on_holding": bool(getattr(t, "blocked", False)),
        "holding_reason": getattr(t, "blocked_reason", None) or "",
        "holding_until": t.blocked_until.isoformat() if getattr(t, "blocked_until", None) else None,
        "jira_key": jira_key,
        "checklistDone": sum(1 for i in checklist if i.get("done")),
        "checklistTotal": len(checklist),
    }


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db), week_offset: int = 0) -> dict:
    try:
        from sqlalchemy import delete as _sa_delete
        _valid_result = await db.execute(
            select(Task.id).where(Task.status.notin_(["done", "cancelled", "dropped"]))
        )
        _valid_ids = {row[0] for row in _valid_result.all()}
        _orphan_blocks = await db.execute(
            select(AgendaBlock).where(
                AgendaBlock.task_id.isnot(None),
                AgendaBlock.source != "gcal",
            )
        )
        for _block in _orphan_blocks.scalars().all():
            if _block.task_id not in _valid_ids:
                await db.delete(_block)
        _wrong_type = select(Task.id).where(Task.task_type.in_(["project", "deliverable"]))
        await db.execute(_sa_delete(AgendaBlock).where(AgendaBlock.task_id.in_(_wrong_type)))
        await db.commit()
    except Exception:
        logger.exception("Erro ao limpar blocos órfãos no state")

    focus, next_task = await _build_focus_v3(db)
    today_tasks = await _build_today_tasks(db)
    active_queue = await _build_active_queue(db)
    agenda_data = await _build_agenda_payload(db, week_offset)

    if not agenda_data.get("suggestedBlocks") and week_offset == 0:
        try:
            from app.services.scheduler import rebuild_week_schedule
            ws = agenda_data.get("_monday")
            we = agenda_data.get("_friday")
            if ws and we:
                await rebuild_week_schedule(db, ws, we)
                agenda_data = await _build_agenda_payload(db, week_offset)
        except Exception:
            logger.exception("Erro ao recalcular agenda no state")

    agenda_data.pop("_monday", None)
    agenda_data.pop("_friday", None)

    projects = await _get_project_names(db)

    suggested = agenda_data.get("suggestedBlocks", [])
    if next_task and suggested:
        next_task["startTime"] = find_next_block_for_task(suggested, next_task["taskId"])

    risk_alert = agenda_data.pop("_riskAlert", None)

    deficit = _calc_deficit(active_queue, agenda_data, week_offset)

    personal_suggestion = await _build_personal_suggestion(db)
    import os
    state = {
        "focus": focus,
        "nextTask": next_task,
        "today": today_tasks,
        "activeQueue": active_queue,
        "agenda": agenda_data,
        "projects": projects,
        "riskAlert": risk_alert,
        "deficit": deficit,
        "personalSuggestion": personal_suggestion,
        "xp": await _build_xp_payload(db),
        "dumpLibrary": await _build_dump_library(db),
        "jiraUrl": os.getenv("JIRA_URL", ""),
    }
    return sanitize_json_strings(state)


@router.get("/task/{task_id}")
async def get_task_detail(task_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    project, task_name = _parse_project_task(task.title)
    checklist = getattr(task, "checklist_json", None) or []
    notes_list = getattr(task, "notes_json", None) or []
    dl_type = getattr(task, "deadline_type", None) or "soft"

    related_dumps: list[dict] = []
    if project or task_name:
        keywords = [w for w in (project + " " + task_name).lower().split() if len(w) > 3]
        if keywords:
            dumps_result = await db.execute(
                select(DumpItem).where(DumpItem.status != "archived").order_by(DumpItem.created_at.desc()).limit(100)
            )
            for d in dumps_result.scalars().all():
                text = ((d.rewritten_title or "") + " " + (d.raw_text or "")).lower()
                if any(kw in text for kw in keywords):
                    related_dumps.append({"id": str(d.id), "title": d.rewritten_title or d.raw_text[:60] or ""})
                    if len(related_dumps) >= 3:
                        break

    history_parts = []
    if task.created_at:
        history_parts.append(f"criada {task.created_at.strftime('%d/%m')}")
    if task.deadline:
        history_parts.append(f"prazo {task.deadline.strftime('%d/%m')}")
    if task.actual_minutes:
        history_parts.append(f"{task.actual_minutes // 60}h trabalhadas")
    elif task.estimated_minutes:
        history_parts.append(f"estimativa {task.estimated_minutes // 60}h")

    return {
        "id": str(task.id),
        "project": project,
        "taskName": task_name,
        "fullTitle": task.title,
        "deadline": _serialize_deadline(task.deadline),
        "deadlineHuman": _humanize_deadline(task.deadline),
        "deadlineType": dl_type,
        "estimatedMinutes": task.estimated_minutes or 120,
        "actualMinutes": task.actual_minutes,
        "status": task.status,
        "checklist": checklist,
        "notes": notes_list,
        "relatedDumps": related_dumps,
        "history": " · ".join(history_parts),
        "blocked": getattr(task, 'blocked', False) or False,
        "blocked_reason": getattr(task, 'blocked_reason', None) or "",
        "blocked_until": task.blocked_until.isoformat() if getattr(task, 'blocked_until', None) else None,
    }


@router.get("/projects")
async def get_projects(db: AsyncSession = Depends(get_db)) -> list:
    from datetime import datetime as _dt
    result = await db.execute(
        select(Task)
        .where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.times_planned.asc().nulls_last(), Task.deadline.asc().nulls_last())
    )
    all_tasks = result.scalars().all()

    tasks_by_id = {str(t.id): t for t in all_tasks}
    children_of: dict[str | None, list[Task]] = {}
    for t in all_tasks:
        pid = str(t.parent_id) if t.parent_id else None
        children_of.setdefault(pid, []).append(t)

    def _task_dict(t: Task) -> dict:
        project, task_name = _parse_project_task(t.title)
        checklist = getattr(t, "checklist_json", None) or []
        task_type = getattr(t, "task_type", "task") or "task"
        jira_key = (t.origin_ref or "") if (getattr(t, "origin", "") == "jira") else ""
        dl_type = getattr(t, "deadline_type", None) or "soft"
        return {
            "id": str(t.id),
            "name": t.title,
            "title": t.title,
            "taskName": task_name,
            "project": project,
            "type": task_type,
            "task_type": task_type,
            "status": t.status,
            "deadline": _serialize_deadline(t.deadline),
            "deadline_human": _humanize_deadline(t.deadline),
            "deadlineHuman": _humanize_deadline(t.deadline),
            "deadline_type": dl_type,
            "deadlineType": dl_type,
            "estimated_minutes": t.estimated_minutes,
            "actual_minutes": t.actual_minutes,
            "parent_id": str(t.parent_id) if t.parent_id else None,
            "jira_key": jira_key,
            "checklistDone": sum(1 for i in checklist if i.get("done")),
            "checklistTotal": len(checklist),
            "blocked": getattr(t, "blocked", False) or False,
            "sort_order": getattr(t, "times_planned", 0) or 0,
        }

    def _collect_leaf_tasks(t: Task) -> list[Task]:
        leaves = []
        for kid in children_of.get(str(t.id), []):
            tt = getattr(kid, "task_type", "task") or "task"
            if tt == "task":
                leaves.append(kid)
            else:
                leaves.extend(_collect_leaf_tasks(kid))
        return leaves

    def _build_node(t: Task, depth: int = 0) -> dict:
        node = _task_dict(t)
        kids = sorted(
            children_of.get(str(t.id), []),
            key=lambda x: (getattr(x, "times_planned", 0) or 0, x.deadline or _dt.max),
        )
        built_kids = [_build_node(k, depth + 1) for k in kids]
        node["children"] = built_kids
        if depth == 0:
            node["deliverables"] = built_kids
        elif depth == 1:
            node["tasks"] = built_kids

        leaf_tasks = _collect_leaf_tasks(t)
        node["active_count"] = sum(1 for lt in leaf_tasks if lt.status in ACTIVE_STATUSES)

        if node["active_count"] > 0:
            node["derived_status"] = "active"
        elif leaf_tasks and all(lt.status == "done" for lt in leaf_tasks):
            node["derived_status"] = "done"
        else:
            node["derived_status"] = t.status or "active"

        return node

    projects = [t for t in all_tasks if (getattr(t, "task_type", "task") or "task") == "project"]
    implicit_roots = [
        t for t in all_tasks
        if (getattr(t, "task_type", "task") or "task") not in ("project",)
        and not t.parent_id
        and children_of.get(str(t.id))
    ]
    roots = sorted(set(projects + implicit_roots), key=lambda t: (t.deadline or _dt.max))

    tree = [_build_node(t, 0) for t in roots]

    root_project_ids = {str(t.id) for t in roots}
    orphans = [
        _task_dict(t) for t in all_tasks
        if not t.parent_id
        and str(t.id) not in root_project_ids
    ]
    if orphans:
        tree.append({
            "id": None,
            "name": "Sem projeto",
            "title": "Sem projeto",
            "type": "none",
            "task_type": "none",
            "status": "active",
            "children": orphans,
            "deliverables": orphans,
        })

    return tree


@router.get("/all-tasks")
async def get_all_tasks(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.deadline.asc().nulls_last(), Task.priority.asc().nulls_last())
    )
    tasks = result.scalars().all()
    parent_map = await _prefetch_parents(tasks, db)

    active, on_holding, backlog = [], [], []
    for t in tasks:
        status = t.status or "active"
        is_blocked = bool(getattr(t, "blocked", False))
        if is_blocked or status == "on_holding":
            on_holding.append(_task_to_flat(t, parent_map))
        elif status in ("pending", "in_progress", "active"):
            active.append(_task_to_flat(t, parent_map))
        elif status == "backlog":
            backlog.append(_task_to_flat(t, parent_map))

    return {"active": active, "onHolding": on_holding, "backlog": backlog}


@router.get("/projects/completed")
async def get_completed_projects(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(Task).where(Task.status.in_(["done", "completed"]))
        .order_by(Task.completed_at.desc().nulls_last())
        .limit(100)
    )
    tasks = result.scalars().all()
    return [_task_to_flat(t) for t in tasks]


@router.get("/hierarchy/projects")
async def list_projects_for_select(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(Task)
        .where(Task.task_type == "project")
        .where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.title.asc())
    )
    return [{"id": str(t.id), "name": t.title} for t in result.scalars().all()]


@router.get("/hierarchy/deliverables")
async def list_deliverables_for_select(project_id: str = "", db: AsyncSession = Depends(get_db)) -> list:
    query = (
        select(Task)
        .where(Task.task_type == "deliverable")
        .where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.title.asc())
    )
    if project_id:
        try:
            query = query.where(Task.parent_id == UUID(project_id))
        except ValueError:
            pass
    result = await db.execute(query)
    return [
        {"id": str(t.id), "name": t.title, "project_id": str(t.parent_id) if t.parent_id else None}
        for t in result.scalars().all()
    ]


@router.get("/project-suggestions")
async def project_suggestions(q: str = "", db: AsyncSession = Depends(get_db)) -> list:
    stmt = (
        select(Task)
        .where(Task.task_type.in_(["project", "deliverable"]))
        .where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .where(Task.title.ilike(f"%{q}%"))
        .order_by(Task.title.asc())
        .limit(10)
    )
    result = await db.execute(stmt)
    tasks = result.scalars().all()

    parent_ids = [t.parent_id for t in tasks if t.parent_id]
    parent_tasks: dict[str, Task] = {}
    if parent_ids:
        p_result = await db.execute(select(Task).where(Task.id.in_(parent_ids)))
        for pt in p_result.scalars().all():
            parent_tasks[str(pt.id)] = pt

    suggestions = []
    for t in tasks:
        parent_name = None
        if t.parent_id and str(t.parent_id) in parent_tasks:
            parent_name = parent_tasks[str(t.parent_id)].title
        suggestions.append({
            "id": str(t.id),
            "name": t.title,
            "type": t.task_type,
            "parentName": parent_name,
        })
    return suggestions


@router.get("/focus")
async def dashboard_focus(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_focus_snapshot(db)


@router.get("/tomorrow")
async def dashboard_tomorrow(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_tomorrow_board(db)


@router.get("/night-summary")
async def night_summary(db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.time_utils import now_brt
    from datetime import time

    agora = now_brt()
    hoje = agora.date()

    result = await db.execute(
        select(Task).where(Task.status.in_(["done", "completed"]))
    )
    all_done = result.scalars().all()
    done_today = []
    for t in all_done:
        completed = t.completed_at
        if completed:
            try:
                up_date = completed.date() if hasattr(completed, 'date') else completed
                if up_date == hoje:
                    done_today.append(t)
            except Exception:
                logger.warning("Erro ao comparar data de completed_at para task %s", t.id)

    result2 = await db.execute(
        select(Task)
        .where(Task.status.in_(list(ACTIVE_STATUSES)))
        .where(Task.category.not_like("personal%"))
    )
    pendentes = result2.scalars().all()

    amanha = hoje + timedelta(days=1)
    if amanha.weekday() >= 5:
        amanha = amanha + timedelta(days=(7 - amanha.weekday()))

    amanha_tasks = []
    for t in pendentes:
        dl = t.deadline
        if dl:
            try:
                dl_date = dl.date() if hasattr(dl, 'date') else dl
                if dl_date <= amanha:
                    title = t.title or ''
                    if ' | ' in title:
                        parts = title.split(' | ', 1)
                        name = f"{parts[0]} | {parts[1]}"
                    else:
                        name = title
                    amanha_tasks.append(name)
            except Exception:
                logger.warning("Erro ao processar deadline para task %s", t.id)

    summary = f"✅ {len(done_today)} task{'s' if len(done_today) != 1 else ''} concluída{'s' if len(done_today) != 1 else ''} hoje<br>"
    summary += f"⏳ {len(pendentes)} pendente{'s' if len(pendentes) != 1 else ''}"

    tomorrow = ""
    if amanha_tasks:
        tomorrow = "📋 amanhã:<br>" + "<br>".join(["• " + n for n in amanha_tasks[:5]])
    else:
        tomorrow = "✨ nada urgente amanhã"

    return {"summary": summary, "tomorrow": tomorrow}


@router.get("/jira/issues")
async def jira_list_issues(db: AsyncSession = Depends(get_db)) -> dict:
    import base64
    import httpx
    from app.config import settings

    def _jira_configured() -> bool:
        return bool(settings.jira_base_url and settings.jira_email and settings.jira_api_token)

    def _jira_auth_headers() -> dict:
        token = base64.b64encode(
            f"{settings.jira_email}:{settings.jira_api_token}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}

    linked_result = await db.execute(
        select(Task.origin_ref).where(Task.origin == "jira").where(Task.origin_ref.is_not(None))
    )
    linked_keys = {row[0] for row in linked_result.all()}

    jql = "assignee = currentUser() AND status != Done ORDER BY duedate ASC"
    url = f"{settings.jira_base_url.rstrip('/')}/rest/api/3/search/jql"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                headers=_jira_auth_headers(),
                json={
                    "jql": jql,
                    "maxResults": 50,
                    "fields": ["summary", "status", "duedate", "priority", "description"],
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.exception("Erro ao listar issues do Jira")
        return {"status": "error", "message": str(e)}

    def _jira_issue_to_dict(issue: dict, linked_keys: set) -> dict:
        fields = issue.get("fields", {})
        key = issue.get("key", "")
        due = fields.get("duedate")
        priority_name = (fields.get("priority") or {}).get("name", "Medium")
        description_raw = fields.get("description") or {}
        desc_text = ""
        if isinstance(description_raw, dict):
            try:
                for block in description_raw.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            desc_text += inline.get("text", "")
            except Exception:
                logger.warning("Erro ao parsear descrição do Jira issue %s", key)
        elif isinstance(description_raw, str):
            desc_text = description_raw
        return {
            "key": key,
            "summary": fields.get("summary", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "dueDate": due,
            "priority": priority_name,
            "description": desc_text[:200],
            "linked": key in linked_keys,
        }

    issues = [_jira_issue_to_dict(i, linked_keys) for i in data.get("issues", [])]
    return {"status": "ok", "issues": issues, "total": len(issues)}


@router.get("/chat/history")
async def get_chat_history(limit: int = 50, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import ChatMessage
    result = await db.execute(
        select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(limit)
    )
    messages = list(reversed(result.scalars().all()))
    return {"messages": [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "intent": m.intent,
            "result": m.result_data,
            "time": m.created_at.strftime("%H:%M") if m.created_at else "",
        }
        for m in messages
    ]}


@router.get("/personal")
async def get_personal_items(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(Task)
        .where(Task.category.like("personal%"))
        .order_by(Task.created_at.desc())
    )
    tasks = result.scalars().all()
    items = []
    for t in tasks:
        items.append({
            "id": str(t.id),
            "title": t.title or "",
            "text": t.title or "",
            "category": t.category or "personal_ideias",
            "status": t.status or "pending",
            "deadline": _serialize_deadline(t.deadline),
        })
    return {"items": items}


@router.get("/dumps")
async def get_dumps(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(DumpItem)
        .where(DumpItem.status != "archived")
        .order_by(DumpItem.created_at.desc())
        .limit(100)
    )
    items = result.scalars().all()
    return {"items": [
        {
            "id": str(d.id),
            "text": d.rewritten_title or d.raw_text or "",
            "content": d.rewritten_title or d.raw_text or "",
            "type": d.category or "anotacao",
            "category": d.category or "anotacao",
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in items
    ]}
