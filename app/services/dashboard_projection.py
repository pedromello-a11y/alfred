from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, DumpItem, Task
from app.services import gcal_client, task_manager
from app.services.dump_manager import classify_dump


def _priority_label(p: int | None) -> str:
    if p is None:
        return "média"
    if p <= 2:
        return "alta"
    if p == 3:
        return "média"
    return "baixa"


def _priority_dot(p: int | None) -> str:
    if p is None:
        return "md"
    if p <= 2:
        return "hi"
    if p == 3:
        return "md"
    return "lo"


def _estimate_label(minutes: int | None) -> str:
    if not minutes:
        return "?"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"~{h}h{m:02d}m"
    if h:
        return f"~{h}h"
    return f"~{m}min"


def _deadline_label(dl: datetime | None) -> str:
    if dl is None:
        return "sem prazo"
    today = date.today()
    delta = (dl.date() - today).days
    if delta < 0:
        return "atrasado"
    if delta == 0:
        return "hoje"
    if delta == 1:
        return "amanhã"
    if delta <= 7:
        return "esta semana"
    return dl.strftime("%d/%m")


def _serialize_task(t: Task, *, current: bool = False) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "title": t.title,
        "status": t.status,
        "priority": _priority_dot(t.priority),
        "priorityLabel": _priority_label(t.priority),
        "estimate": _estimate_label(t.estimated_minutes),
        "deadline": _deadline_label(t.deadline),
        "current": current,
    }


def _serialize_block(block: dict[str, Any]) -> dict[str, Any]:
    start_at = block.get("start_at")
    end_at = block.get("end_at")
    return {
        "title": block.get("title", ""),
        "type": block.get("type", "focus"),
        "source": block.get("source", "manual"),
        "start": start_at.strftime("%H:%M") if isinstance(start_at, datetime) else "",
        "end": end_at.strftime("%H:%M") if isinstance(end_at, datetime) else "",
        "notes": block.get("notes", ""),
    }


async def _today_agenda_blocks(db: AsyncSession) -> list[dict[str, Any]]:
    today = date.today()
    start_day = datetime.combine(today, time.min)
    end_day = datetime.combine(today + timedelta(days=1), time.min)
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= start_day)
        .where(AgendaBlock.start_at < end_day)
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = []
    for block in result.scalars().all():
        blocks.append(
            {
                "title": block.title,
                "type": block.block_type,
                "source": block.source or "manual",
                "start_at": block.start_at,
                "end_at": block.end_at,
                "notes": block.notes or "",
            }
        )
    return blocks


async def _today_gcal_blocks() -> list[dict[str, Any]]:
    try:
        raw = await gcal_client.get_today_events()
    except Exception:
        return []

    blocks = []
    for ev in raw or []:
        try:
            start_at = datetime.fromisoformat(ev.get("start", ""))
            end_at = datetime.fromisoformat(ev.get("end", ""))
        except Exception:
            continue
        blocks.append(
            {
                "title": ev.get("title", ""),
                "type": "meeting",
                "source": "gcal",
                "start_at": start_at,
                "end_at": end_at,
                "notes": ev.get("description", "") or "",
            }
        )
    return blocks


async def get_focus_board(db: AsyncSession) -> dict[str, Any]:
    active = list(await task_manager.get_active_tasks(db))
    today = date.today()
    now = datetime.now()

    manual_blocks = await _today_agenda_blocks(db)
    gcal_blocks = await _today_gcal_blocks()

    # Normalize all block datetimes to naive local time for consistent comparison
    def _to_naive(dt: datetime) -> datetime:
        if dt is None:
            return datetime.max
        if dt.tzinfo is not None:
            from datetime import timezone as _tz
            return dt.astimezone().replace(tzinfo=None)
        return dt

    for b in manual_blocks + gcal_blocks:
        b["start_at"] = _to_naive(b.get("start_at"))
        b["end_at"] = _to_naive(b.get("end_at") or datetime.max)

    timeline = sorted(manual_blocks + gcal_blocks, key=lambda item: item.get("start_at") or datetime.max)

    current_block = next((b for b in timeline if b["start_at"] <= now < b["end_at"]), None)
    next_block = next((b for b in timeline if b["start_at"] > now), None)

    today_tasks = [
        _serialize_task(task, current=(idx == 0))
        for idx, task in enumerate(
            [
                t for t in active
                if t.status == "in_progress"
                or (t.deadline and t.deadline.date() == today)
                or t.last_planned == today
            ][:6]
        )
    ]

    alerts = []
    for task in active:
        if task.deadline and task.deadline.date() < today:
            alerts.append(f"Atrasado: {task.title}")
        elif task.deadline and task.deadline.date() == today:
            alerts.append(f"Vence hoje: {task.title}")
    alerts = alerts[:5]

    meetings = [_serialize_block(block) for block in timeline if block.get("type") == "meeting"]

    return {
        "currentBlock": _serialize_block(current_block) if current_block else None,
        "nextBlock": _serialize_block(next_block) if next_block else None,
        "timeline": [_serialize_block(block) for block in timeline],
        "todayTasks": today_tasks,
        "meetings": meetings,
        "alerts": alerts,
    }


async def get_horizon_board(db: AsyncSession) -> dict[str, Any]:
    active = list(await task_manager.get_active_tasks(db))
    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)

    tomorrow_tasks: list[dict[str, Any]] = []
    week_tasks: list[dict[str, Any]] = []
    later_tasks: list[dict[str, Any]] = []

    for task in active:
        if task.deadline:
            due = task.deadline.date()
            if due == tomorrow:
                tomorrow_tasks.append(_serialize_task(task))
                continue
            if tomorrow < due <= week_end:
                week_tasks.append(_serialize_task(task))
                continue
            if due > week_end:
                later_tasks.append(_serialize_task(task))
                continue
        if task.last_planned == tomorrow:
            tomorrow_tasks.append(_serialize_task(task))
        elif task.last_planned and today < task.last_planned <= week_end:
            week_tasks.append(_serialize_task(task))
        elif task.status == "pending":
            later_tasks.append(_serialize_task(task))

    return {
        "tomorrow": tomorrow_tasks[:6],
        "thisWeek": week_tasks[:8],
        "later": later_tasks[:8],
    }


async def get_active_queue(db: AsyncSession, limit: int = 12) -> list[dict[str, Any]]:
    active = list(await task_manager.get_active_tasks(db))
    return [_serialize_task(task, current=(idx == 0)) for idx, task in enumerate(active[:limit])]


async def get_dump_library(db: AsyncSession, limit: int = 50) -> dict[str, Any]:
    explicit_q = await db.execute(
        select(DumpItem)
        .order_by(DumpItem.created_at.desc())
        .limit(limit)
    )
    explicit_items = list(explicit_q.scalars().all())
    explicit_source_ids = {item.source_task_id for item in explicit_items if item.source_task_id}

    legacy_q = await db.execute(
        select(Task)
        .where(Task.status == "dump")
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    legacy_tasks = [task for task in legacy_q.scalars().all() if task.id not in explicit_source_ids]

    items: list[dict[str, Any]] = []
    for item in explicit_items:
        items.append(
            {
                "id": f"dump:{item.id}",
                "title": item.rewritten_title,
                "summary": item.summary or item.raw_text,
                "rawText": item.raw_text,
                "category": item.category or "desconhecido",
                "subcategory": item.subcategory,
                "confidence": item.confidence or 0.0,
                "status": item.status,
                "createdAt": item.created_at.isoformat() if item.created_at else "",
                "source": item.source or "manual",
            }
        )

    for task in legacy_tasks:
        classified = classify_dump(task.title)
        items.append(
            {
                "id": f"task:{task.id}",
                "title": classified.rewritten_title,
                "summary": classified.summary,
                "rawText": task.title,
                "category": classified.category,
                "subcategory": classified.subcategory,
                "confidence": classified.confidence,
                "status": classified.status,
                "createdAt": task.created_at.isoformat() if task.created_at else "",
                "source": task.origin or "legacy_task",
            }
        )

    items.sort(key=lambda item: item.get("createdAt", ""), reverse=True)

    category_counts = Counter(item["category"] or "desconhecido" for item in items)
    categories = [
        {"name": name, "count": count}
        for name, count in category_counts.most_common()
    ]
    needs_review = [item for item in items if item.get("status") == "unknown" or item.get("confidence", 0) < 0.5]

    return {
        "categories": categories,
        "recent": items[:20],
        "needsReview": needs_review[:12],
        "total": len(items),
    }
