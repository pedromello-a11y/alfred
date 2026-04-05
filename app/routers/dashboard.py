"""Router do dashboard — Alfred v3."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AgendaBlock, DumpItem, PlayerStat, Streak, Task
from app.services import task_manager
from app.services.block_engine import build_suggested_blocks, find_next_block_for_task, recalculate_suggestions
from app.services.focus_snapshot import build_focus_snapshot
from app.services.task_manager import calculate_level, xp_progress_in_level
from app.services.text_utils import sanitize_json_strings
from app.services.time_utils import today_brt
from app.services.tomorrow_board import build_tomorrow_board

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# ── helpers ────────────────────────────────────────────────────────────────

def _today_brt() -> date:
    return today_brt()


def _now_brt() -> datetime:
    from app.services.time_utils import now_brt
    return now_brt()


def _parse_project_task(title: str) -> tuple[str, str]:
    if "|" in (title or ""):
        p, t = title.split("|", 1)
        return p.strip(), t.strip()
    return "", (title or "").strip()


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


def _task_to_queue_item(task: Task, today: date) -> dict:
    project, task_name = _parse_project_task(task.title)
    dl_type = getattr(task, "deadline_type", None) or "soft"
    checklist = getattr(task, "checklist_json", None) or []
    return {
        "id": str(task.id),
        "project": project,
        "taskName": task_name,
        "fullTitle": task.title,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "deadlineRaw": task.deadline.isoformat() if task.deadline else None,
        "deadlineHuman": _humanize_deadline(task.deadline),
        "deadlineType": dl_type,
        "status": task.status,
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
    # Filter out personal tasks more robustly
    work_tasks = [t for t in tasks if not (t.category or "").startswith("personal")]
    if not work_tasks:
        return {"status": "empty"}, None

    focus_task = work_tasks[0]
    project, task_name = _parse_project_task(focus_task.title)
    dl_type = getattr(focus_task, "deadline_type", None) or "soft"

    # Find current agenda block
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

    # Determine status
    has_deadline_today = False
    if focus_task.deadline:
        try:
            dl_date = focus_task.deadline.date() if hasattr(focus_task.deadline, "date") else focus_task.deadline
            has_deadline_today = dl_date == today
        except Exception:
            pass

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
        "deadline": focus_task.deadline.isoformat() if focus_task.deadline else None,
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
            "deadline": next_task_obj.deadline.isoformat() if next_task_obj.deadline else None,
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

    # Tasks com deadline hoje
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress", "active", "done", "completed")))
        .where(Task.deadline >= today_start)
        .where(Task.deadline < today_end)
        .order_by(Task.deadline.asc())
    )
    tasks = result.scalars().all()

    # AgendaBlocks de hoje (excluindo gcal puro sem task_id)
    blocks_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= today_start)
        .where(AgendaBlock.start_at < today_end)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = blocks_result.scalars().all()

    # Mapear task_id -> bloco
    task_block_map: dict[str, AgendaBlock] = {}
    for b in blocks:
        if b.task_id:
            task_block_map[str(b.task_id)] = b

    # Calcular horas livres restantes (entre agora e 20h)
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
            "deadline": task.deadline.isoformat() if task.deadline else None,
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

    # Ordenar: com horário alocado primeiro (por hora), sem horário por deadline
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
        .order_by(Task.deadline.asc().nulls_last(), Task.priority.asc().nulls_last())
        .limit(50)
    )
    tasks = result.scalars().all()
    return [_task_to_queue_item(t, today) for t in tasks]


def _current_workweek_bounds(ref: date | None = None) -> tuple[date, date]:
    today = ref or _today_brt()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


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

        # Blocos alfred (suggested/quick/pinned) → vão em suggestedBlocks com blockId
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
            # GCal/manual → vão em days.events
            days[dow].append({
                "title": block.title,
                "start": block.start_at.strftime("%H:%M"),
                "time": block.start_at.strftime("%H:%M"),
                "end": block.end_at.strftime("%H:%M") if block.end_at else "",
                "type": type_map.get(btype, "focus"),
                "source": source,
            })

    deadlines = await _build_agenda_deadlines(db, monday, friday)

    # Motor de blocos sugeridos ao vivo (fallback se banco estiver vazio de alfred blocks)
    if not alfred_blocks:
        try:
            suggested, risk_alert = await build_suggested_blocks(db, monday, friday)
        except Exception:
            suggested, risk_alert = [], None
    else:
        suggested = alfred_blocks
        risk_alert = None
        # Calcular risco manualmente
        try:
            _, risk_alert = await build_suggested_blocks(db, monday, friday)
        except Exception:
            risk_alert = None

    return {
        "days": [{"day": d, "events": events} for d, events in days.items()],
        "suggestedBlocks": suggested,
        "pauses": [],
        "deadlines": deadlines,
        "weekStart": monday.isoformat(),
        "weekEnd": friday.isoformat(),
        "_riskAlert": risk_alert,  # passado para cima pelo /state
    }


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


async def _parse_task_with_ai(raw_text: str) -> dict:
    try:
        today = _today_brt()
        dias_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        today_name = dias_pt[today.weekday()]
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        prompt = (
            f'Analise esta descrição de tarefa e extraia as informações em JSON.\n\n'
            f'Texto: "{raw_text}"\n\n'
            f'Data atual: {today.isoformat()} ({today_name})\n\n'
            'Retorne APENAS um JSON válido (sem markdown) com:\n'
            '- project: nome do projeto em maiúsculas (string ou "")\n'
            '- title: nome da tarefa (string)\n'
            '- deadline: prazo ISO 8601 ou null\n'
            '- estimate: estimativa em minutos (int, padrão 120)\n'
            '- deadlineType: "hard" se é entrega/cliente/fixo, "soft" se é meta pessoal'
        )
        msg = await client.messages.create(
            model=settings.model_fast,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return {"project": "", "title": raw_text, "deadline": None, "estimate": 120, "deadlineType": "soft"}


# ── endpoints ──────────────────────────────────────────────────────────────

def _calc_deficit(active_queue: list[dict], agenda_data: dict, week_offset: int) -> dict:
    """Calcula déficit de horas para a semana exibida."""
    from app.services.time_utils import today_brt as _tb
    today = _tb() + timedelta(weeks=week_offset)
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)

    # Horas necessárias: tasks com deadline até friday
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

    # Horas disponíveis: slots livres estimados via blocos sugeridos
    suggested = agenda_data.get("suggestedBlocks", [])
    total_available = 0
    for b in suggested:
        try:
            s = datetime.strptime(b["start"], "%H:%M")
            e = datetime.strptime(b["end"], "%H:%M")
            total_available += int((e - s).total_seconds() / 60)
        except Exception:
            pass
    # Fallback: horas úteis brutas se sem sugestões
    if total_available == 0:
        total_available = 5 * 10 * 60  # 5 dias × 10h

    needed_h = round(total_needed / 60, 1)
    available_h = round(total_available / 60, 1)
    overflow_h = round(max(0, needed_h - available_h), 1)

    # Ordenar movable por deadline mais distante
    movable.sort(key=lambda i: i.get("deadline") or "9999", reverse=True)

    return {
        "totalNeeded": needed_h,
        "totalAvailable": available_h,
        "overflow": overflow_h,
        "movableTasks": movable[:5],
    }


async def _build_personal_suggestion(db: AsyncSession) -> dict | None:
    """Returns one pending personal task to suggest during free time."""
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


@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db), week_offset: int = 0) -> dict:
    # One-time cleanup of orphan blocks on each state load
    try:
        from sqlalchemy import delete as sa_delete
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
        await db.commit()
    except Exception:
        pass

    focus, next_task = await _build_focus_v3(db)
    today_tasks = await _build_today_tasks(db)
    active_queue = await _build_active_queue(db)
    agenda_data = await _build_agenda_payload(db, week_offset)
    projects = await _get_project_names(db)

    # Preenche startTime do nextTask com o próximo bloco sugerido
    suggested = agenda_data.get("suggestedBlocks", [])
    if next_task and suggested:
        next_task["startTime"] = find_next_block_for_task(suggested, next_task["taskId"])

    # Extrai riskAlert que veio embutido no agenda_data
    risk_alert = agenda_data.pop("_riskAlert", None)

    # Calcular deficit para aba agenda
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
        "jiraUrl": os.getenv("JIRA_URL", settings.jira_base_url or ""),
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

    # Related dumps — search by project/task name keywords
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

    # Build history string
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
        "deadline": task.deadline.isoformat() if task.deadline else None,
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


@router.post("/task/{task_id}/checklist")
async def manage_checklist(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    checklist: list = list(getattr(task, "checklist_json", None) or [])
    action = body.get("action", "")

    if action == "add":
        text = (body.get("text") or "").strip()
        if text:
            checklist.append({"text": text, "done": False})
    elif action == "toggle":
        idx = body.get("index")
        if idx is not None and 0 <= idx < len(checklist):
            checklist[idx] = {**checklist[idx], "done": not checklist[idx].get("done", False)}
    elif action == "remove":
        idx = body.get("index")
        if idx is not None and 0 <= idx < len(checklist):
            checklist.pop(idx)
    elif action == "edit":
        idx = body.get("index", 0)
        new_text = body.get("text", "")
        if 0 <= idx < len(checklist):
            checklist[idx]["text"] = new_text

    task.checklist_json = checklist
    await db.commit()
    return {"status": "ok", "checklist": checklist}


@router.post("/task/{task_id}/note")
async def add_note(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}

    from app.services.time_utils import now_brt
    now = now_brt()
    notes_list: list = list(getattr(task, "notes_json", None) or [])
    notes_list.insert(0, {"text": text, "created_at": now.strftime("%d/%m %H:%M")})
    task.notes_json = notes_list
    await db.commit()
    return {"status": "ok", "notes": notes_list}


@router.post("/task/{task_id}/complete")
async def complete_task_v3(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    task.status = "done"
    task.completed_at = datetime.now(timezone.utc)
    actual = body.get("actual_minutes")
    if actual:
        task.actual_minutes = int(actual)

    # Cascade: remove agenda blocks for this task
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))

    await db.commit()

    # Log média do projeto para uso futuro
    project = getattr(task, "project", None) or (task.title.split("|")[0].strip() if task.title and "|" in task.title else "")
    if project and task.actual_minutes:
        from sqlalchemy import and_
        import logging as _logging
        completed = await db.execute(
            select(Task).where(
                and_(
                    Task.status.in_(["done", "completed"]),
                    Task.actual_minutes.isnot(None),
                )
            )
        )
        done_tasks = [t for t in completed.scalars().all() if (t.title or "").startswith(project)]
        if len(done_tasks) >= 3:
            avg = sum(t.actual_minutes for t in done_tasks) / len(done_tasks)
            _logging.getLogger(__name__).info(f"Projeto {project}: média real {avg:.0f}min ({len(done_tasks)} tasks)")

    return {"status": "ok", "title": task.title}


@router.post("/pause")
async def insert_pause(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    duration = int(body.get("duration_minutes", 15))
    from app.services.time_utils import now_brt_naive
    now = now_brt_naive()
    end = now + timedelta(minutes=duration)
    pause_block = AgendaBlock(
        title=f"Pausa {duration}min",
        start_at=now,
        end_at=end,
        block_type="break",
        source="manual",
        status="planned",
    )
    db.add(pause_block)
    await db.commit()
    return {"status": "ok", "duration_minutes": duration}


@router.post("/task/create-smart")
async def create_task_smart(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    raw_text = (body.get("raw_text") or "").strip()
    if not raw_text:
        return {"status": "error", "message": "raw_text required"}
    parsed = await _parse_task_with_ai(raw_text)
    return {"parsed": parsed, "needs_confirmation": True}


@router.post("/task/create-smart/confirm")
async def confirm_smart_task(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    project = (body.get("project") or "").strip()
    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "title required"}
    full_title = f"{project} | {title}" if project else title
    if hasattr(task_manager, "canonicalize_task_title"):
        full_title = task_manager.canonicalize_task_title(full_title)

    deadline = None
    if body.get("deadline"):
        try:
            deadline = datetime.fromisoformat(body["deadline"])
        except ValueError:
            pass

    # Sugerir estimativa baseada no histórico do projeto
    estimate = body.get("estimate") or 120
    if project:
        from sqlalchemy import and_
        completed_hist = await db.execute(
            select(Task).where(
                and_(
                    Task.status.in_(["done", "completed"]),
                    Task.actual_minutes.isnot(None),
                )
            )
        )
        hist_tasks = [t for t in completed_hist.scalars().all() if (t.title or "").startswith(project)]
        if len(hist_tasks) >= 3:
            avg = sum(t.actual_minutes for t in hist_tasks) / len(hist_tasks)
            estimate = int(avg)

    raw_deadline = body.get("deadline")
    if raw_deadline and isinstance(raw_deadline, str) and "T" not in raw_deadline:
        raw_deadline = raw_deadline + "T18:00:00"
    if raw_deadline:
        try:
            deadline = datetime.fromisoformat(raw_deadline)
        except ValueError:
            pass

    task_type = body.get("task_type") or "task"
    if task_type not in ("project", "deliverable", "task"):
        task_type = "task"
    parent_id_val = None
    if body.get("parent_id"):
        try:
            parent_id_val = UUID(body["parent_id"])
        except Exception:
            pass

    incoming_status = body.get("status") or "active"
    if incoming_status in ("pending", "in_progress"):
        incoming_status = "active"

    new_task = Task(
        title=full_title,
        origin="dashboard",
        status=incoming_status,
        estimated_minutes=estimate,
        deadline=deadline,
        category="work",
        task_type=task_type,
        parent_id=parent_id_val,
    )
    if hasattr(new_task, "deadline_type"):
        new_task.deadline_type = body.get("deadline_type") or "soft"
    if hasattr(new_task, "checklist_json"):
        new_task.checklist_json = []
    if hasattr(new_task, "notes_json"):
        new_task.notes_json = []

    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"status": "ok", "id": str(new_task.id), "title": new_task.title}


@router.get("/projects")
async def get_projects(db: AsyncSession = Depends(get_db)) -> list:
    from sqlalchemy import or_, and_
    result = await db.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled", "dropped"]))
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
        return {
            "id": str(t.id),
            "name": t.title,
            "title": t.title,
            "taskName": task_name,
            "project": project,
            "type": task_type,
            "task_type": task_type,
            "status": t.status,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "deadlineHuman": _humanize_deadline(t.deadline),
            "estimated_minutes": t.estimated_minutes,
            "parent_id": str(t.parent_id) if t.parent_id else None,
            "jira_key": jira_key,
            "checklistDone": sum(1 for i in checklist if i.get("done")),
            "checklistTotal": len(checklist),
        }

    def _build_node(t: Task, depth: int = 0) -> dict:
        node = _task_dict(t)
        kids = sorted(children_of.get(str(t.id), []), key=lambda x: (x.deadline or datetime.max))
        built_kids = [_build_node(k, depth + 1) for k in kids]
        node["children"] = built_kids
        if depth == 0:
            node["deliverables"] = built_kids
        elif depth == 1:
            node["tasks"] = built_kids
        return node

    projects = [t for t in all_tasks if (getattr(t, "task_type", "task") or "task") == "project"]
    # Tasks que têm filhos mas task_type != 'project'
    implicit_roots = [
        t for t in all_tasks
        if (getattr(t, "task_type", "task") or "task") not in ("project",)
        and not t.parent_id
        and children_of.get(str(t.id))
    ]
    roots = sorted(set(projects + implicit_roots), key=lambda t: (t.deadline or datetime.max))

    tree = [_build_node(t, 0) for t in roots]

    # Orphans: parent_id IS NULL, task_type='task', no children
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


def _task_to_flat(t: Task) -> dict:
    project, task_name = _parse_project_task(t.title)
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
        "deadline": t.deadline.isoformat() if t.deadline else None,
        "deadlineHuman": _humanize_deadline(t.deadline),
        "estimated_minutes": t.estimated_minutes,
        "on_holding": bool(getattr(t, "blocked", False)),
        "holding_reason": getattr(t, "blocked_reason", None) or "",
        "holding_until": t.blocked_until.isoformat() if getattr(t, "blocked_until", None) else None,
        "jira_key": jira_key,
        "checklistDone": sum(1 for i in checklist if i.get("done")),
        "checklistTotal": len(checklist),
    }


@router.get("/all-tasks")
async def get_all_tasks(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.deadline.asc().nulls_last(), Task.priority.asc().nulls_last())
    )
    tasks = result.scalars().all()

    active, on_holding, backlog = [], [], []
    for t in tasks:
        status = t.status or "active"
        is_blocked = bool(getattr(t, "blocked", False))
        if is_blocked or status == "on_holding":
            on_holding.append(_task_to_flat(t))
        elif status in ("pending", "in_progress", "active"):
            active.append(_task_to_flat(t))
        elif status == "backlog":
            backlog.append(_task_to_flat(t))

    return {"active": active, "onHolding": on_holding, "backlog": backlog}


@router.post("/task/{task_id}/rename")
async def rename_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        tid = UUID(task_id)
    except Exception:
        return {"status": "error", "message": "invalid id"}
    result = await db.execute(select(Task).where(Task.id == tid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}
    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "title required"}
    task.title = title
    await db.commit()
    return {"status": "ok"}


@router.post("/task/{task_id}/update")
async def update_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        tid = UUID(task_id)
    except Exception:
        return {"status": "error", "message": "invalid id"}
    result = await db.execute(select(Task).where(Task.id == tid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    if "title" in body:
        task.title = (body["title"] or "").strip()
    if "status" in body:
        s = body["status"]
        if s in ("pending", "in_progress"):
            s = "active"
        task.status = s
        if s in ("done", "cancelled", "dropped"):
            from sqlalchemy import delete as sa_delete
            await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
    if "deadline" in body:
        raw = body["deadline"]
        if raw and isinstance(raw, str) and "T" not in raw:
            raw = raw + "T18:00:00"
        try:
            task.deadline = datetime.fromisoformat(raw) if raw else None
        except Exception:
            pass
    if "deadline_type" in body:
        task.deadline_type = body["deadline_type"]
    if "estimated_minutes" in body:
        task.estimated_minutes = body["estimated_minutes"]
    if "parent_id" in body:
        pid = body["parent_id"]
        task.parent_id = UUID(pid) if pid else None
    if "task_type" in body:
        task.task_type = body["task_type"]
    if "on_holding" in body:
        task.blocked = bool(body["on_holding"])
        if not task.blocked:
            task.status = "active"
        else:
            task.status = "on_holding"
    if "holding_reason" in body:
        task.blocked_reason = body["holding_reason"]
    if "holding_until" in body:
        val = body["holding_until"]
        try:
            from datetime import date as _date
            task.blocked_until = _date.fromisoformat(val) if val else None
        except Exception:
            pass

    await db.commit()
    await db.refresh(task)
    return _task_to_flat(task)


@router.post("/cleanup-orphan-blocks")
async def cleanup_orphan_blocks(db: AsyncSession = Depends(get_db)) -> dict:
    """Remove AgendaBlocks whose task_id points to a deleted/done/cancelled task."""
    from sqlalchemy import delete as sa_delete
    valid_result = await db.execute(
        select(Task.id).where(Task.status.notin_(["done", "cancelled", "dropped"]))
    )
    valid_ids = {row[0] for row in valid_result.all()}
    blocks_result = await db.execute(
        select(AgendaBlock).where(AgendaBlock.task_id.isnot(None))
    )
    deleted = 0
    for block in blocks_result.scalars().all():
        if block.task_id not in valid_ids:
            await db.delete(block)
            deleted += 1
    await db.commit()
    return {"status": "ok", "deleted": deleted}


@router.get("/projects/completed")
async def get_completed_projects(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(Task).where(Task.status.in_(["done", "completed"]))
        .order_by(Task.completed_at.desc().nulls_last())
        .limit(100)
    )
    tasks = result.scalars().all()
    return [_task_to_flat(t) for t in tasks]


@router.get("/project-suggestions")
async def project_suggestions(q: str = "", db: AsyncSession = Depends(get_db)) -> list:
    from sqlalchemy import func as sqlfunc
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

    task_by_id = {str(t.id): t for t in tasks}
    # Also fetch parents for deliverables
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
            "deadline": t.deadline.isoformat() if t.deadline else None,
        })
    return {"items": items}


@router.post("/personal")
async def create_personal_item(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    category = (body.get("category") or "personal").strip()
    if not category.startswith("personal"):
        category = f"personal_{category}"
    new_task = Task(title=text, origin="dashboard", status="pending", category=category)
    if hasattr(new_task, "checklist_json"):
        new_task.checklist_json = []
    if hasattr(new_task, "notes_json"):
        new_task.notes_json = []
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"status": "ok", "id": str(new_task.id)}


@router.post("/personal/{item_id}/edit")
async def edit_personal_item(item_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.title = body.get("title", task.title)
    await db.commit()
    return {"ok": True}


@router.post("/personal/{item_id}/toggle")
async def toggle_personal_item(item_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    done = body.get("done", False)
    task.status = "done" if done else "pending"
    await db.commit()
    return {"ok": True}


@router.post("/personal/{item_id}/delete")
async def delete_personal_item(item_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    await db.delete(task)
    await db.commit()
    return {"ok": True}


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


@router.post("/dump")
async def create_quick_dump(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    category = (body.get("type") or body.get("category") or "anotacao").strip()
    new_dump = DumpItem(
        raw_text=text,
        rewritten_title=text[:100],
        status="categorized",
        source="dashboard",
        category=category,
    )
    db.add(new_dump)
    await db.commit()
    await db.refresh(new_dump)
    return {"status": "ok", "id": str(new_dump.id)}


@router.post("/dump/{dump_id}/delete")
async def delete_dump_item(dump_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        from uuid import UUID as _UUID
        dump_uuid = _UUID(dump_id)
    except ValueError:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid))
    item = result.scalar_one_or_none()
    if not item:
        return {"error": "not found"}
    await db.delete(item)
    await db.commit()
    return {"ok": True}


@router.post("/personal/reorder")
async def reorder_personal(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    ids = body.get("ids", [])
    for i, item_id in enumerate(ids):
        try:
            from uuid import UUID as _UUID
            item_uuid = _UUID(item_id)
        except ValueError:
            continue
        result = await db.execute(select(Task).where(Task.id == item_uuid))
        task = result.scalar_one_or_none()
        if task:
            task.times_planned = i  # reuse times_planned as sort order for personal items
    await db.commit()
    return {"ok": True}


@router.post("/dump/{dump_id}/edit")
async def edit_dump_item(dump_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        from uuid import UUID as _UUID
        dump_uuid = _UUID(dump_id)
    except ValueError:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid))
    item = result.scalar_one_or_none()
    if not item:
        return {"error": "not found"}
    if "rewritten_title" in body:
        item.rewritten_title = body["rewritten_title"]
    if "category" in body:
        item.category = body["category"]
    if "notes" in body:
        item.notes = body["notes"]
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/deadline-type")
async def update_deadline_type(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.deadline_type = body.get("deadline_type", "soft")
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/deadline")
async def update_deadline(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    dl_str = body.get("deadline", "")
    if dl_str:
        try:
            task.deadline = datetime.fromisoformat(dl_str)
        except Exception:
            pass
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/block")
async def block_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    from datetime import datetime, date
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}

    task.blocked = True
    task.blocked_reason = body.get("reason", "")
    until = body.get("blocked_until", None)
    if until:
        try:
            task.blocked_until = datetime.strptime(until, "%Y-%m-%d").date()
        except:
            task.blocked_until = None
    else:
        task.blocked_until = None
    task.blocked_at = datetime.now()
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/unblock")
async def unblock_task(task_id: str, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}

    task.blocked = False
    task.blocked_reason = None
    task.blocked_until = None
    task.blocked_at = None
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/estimate")
async def update_estimate(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.estimated_minutes = body.get("estimated_minutes", 120)
    await db.commit()
    return {"ok": True}


# ── agenda endpoints ──────────────────────────────────────────────────────────

def _week_bounds_from_offset(week_offset: int) -> tuple[date, date]:
    from app.services.time_utils import today_brt as _today_brt_fn
    ref = _today_brt_fn() + timedelta(weeks=week_offset)
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=4)


async def _build_agenda_response(db: AsyncSession, week_offset: int) -> dict:
    """Retorna o payload completo da agenda após recalcular sugestões."""
    week_start, week_end = _week_bounds_from_offset(week_offset)
    suggested = await recalculate_suggestions(db, week_start, week_end)
    agenda = await _build_agenda_payload(db, week_offset)
    risk = agenda.pop("_riskAlert", None)
    return {"ok": True, "agenda": agenda, "riskAlert": risk}


@router.post("/agenda/allocate")
async def agenda_allocate(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    task_id_str = (body.get("task_id") or "").strip()
    day_idx = int(body.get("day", 0))
    start_str = body.get("start", "09:00")
    duration_min = int(body.get("duration_minutes", 120))
    week_offset = int(body.get("week_offset", 0))
    pinned = bool(body.get("pinned", True))

    try:
        task_uuid = UUID(task_id_str)
    except Exception:
        return {"error": "invalid task_id"}

    task_result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = task_result.scalar_one_or_none()
    if not task:
        return {"error": "task not found"}

    week_start, week_end = _week_bounds_from_offset(week_offset)
    day_date = week_start + timedelta(days=day_idx)

    try:
        start_dt = datetime.combine(day_date, datetime.strptime(start_str, "%H:%M").time())
        end_dt = start_dt + timedelta(minutes=duration_min)
    except Exception:
        return {"error": "invalid start time"}

    # Remover bloco alfred pinned anterior desta task nesta semana
    monday_dt = datetime.combine(week_start, datetime.min.time())
    friday_dt = datetime.combine(week_end, datetime.max.time().replace(microsecond=0))
    from sqlalchemy import delete as _sa_delete
    await db.execute(
        _sa_delete(AgendaBlock).where(
            AgendaBlock.task_id == task_uuid,
            AgendaBlock.start_at >= monday_dt,
            AgendaBlock.start_at <= friday_dt,
            AgendaBlock.pinned == True,  # noqa: E712
        )
    )

    new_block = AgendaBlock(
        title=task.title or "",
        start_at=start_dt,
        end_at=end_dt,
        block_type="suggested",
        source="alfred",
        status="planned",
        task_id=task_uuid,
        pinned=pinned,
    )
    db.add(new_block)
    await db.commit()

    return await _build_agenda_response(db, week_offset)


@router.post("/agenda/move")
async def agenda_move(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    block_id_str = (body.get("block_id") or "").strip()
    new_day = int(body.get("new_day", 0))
    new_start_str = body.get("new_start", "09:00")
    week_offset = int(body.get("week_offset", 0))

    try:
        block_uuid = UUID(block_id_str)
    except Exception:
        return {"error": "invalid block_id"}

    block_result = await db.execute(select(AgendaBlock).where(AgendaBlock.id == block_uuid))
    block = block_result.scalar_one_or_none()
    if not block:
        return {"error": "block not found"}

    week_start, _ = _week_bounds_from_offset(week_offset)
    day_date = week_start + timedelta(days=new_day)
    duration = int((block.end_at - block.start_at).total_seconds() / 60)

    try:
        new_start_dt = datetime.combine(day_date, datetime.strptime(new_start_str, "%H:%M").time())
    except Exception:
        return {"error": "invalid new_start"}

    block.start_at = new_start_dt
    block.end_at = new_start_dt + timedelta(minutes=duration)
    block.pinned = True
    await db.commit()

    return await _build_agenda_response(db, week_offset)


@router.post("/agenda/resize")
async def agenda_resize(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    block_id_str = (body.get("block_id") or "").strip()
    new_duration = int(body.get("new_duration_minutes", 60))
    week_offset = int(body.get("week_offset", 0))

    try:
        block_uuid = UUID(block_id_str)
    except Exception:
        return {"error": "invalid block_id"}

    block_result = await db.execute(select(AgendaBlock).where(AgendaBlock.id == block_uuid))
    block = block_result.scalar_one_or_none()
    if not block:
        return {"error": "block not found"}

    block.end_at = block.start_at + timedelta(minutes=max(15, new_duration))
    block.pinned = True
    await db.commit()

    return await _build_agenda_response(db, week_offset)


@router.post("/agenda/block/{block_id}/delete")
async def agenda_block_delete(block_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        block_uuid = UUID(block_id)
    except Exception:
        return {"status": "error", "message": "invalid block_id"}

    block_result = await db.execute(select(AgendaBlock).where(AgendaBlock.id == block_uuid))
    block = block_result.scalar_one_or_none()
    if not block:
        return {"status": "error", "message": "block not found"}

    if block.source == "gcal":
        return {"status": "error", "message": "não é possível remover evento do Google Calendar"}

    week_start = block.start_at.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_start - timedelta(days=week_start.weekday())
    week_end = week_start + timedelta(days=7)

    await db.delete(block)
    await db.commit()
    await recalculate_suggestions(db, week_start, week_end)
    return {"status": "ok"}


@router.post("/agenda/reorganize")
async def agenda_reorganize(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    week_offset = int(body.get("week_offset", 0))
    week_start, week_end = _week_bounds_from_offset(week_offset)
    suggested = await recalculate_suggestions(db, week_start, week_end)
    return {"ok": True, "blocksCreated": len(suggested)}


# ── legacy endpoints (kept for backward compat) ────────────────────────────

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
async def dashboard_action(payload: ActionPayload, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(payload.task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}
    action = (payload.action or "").lower().strip()
    from sqlalchemy import delete as sa_delete
    if action in ("concluida", "concluída", "done"):
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc)
        if payload.note:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            entry = f"[{ts}] {payload.note.strip()}"
            task.notes = f"{task.notes}\n{entry}" if task.notes else entry
        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
        await db.commit()
        return {"status": "ok", "action": "done", "title": task.title}
    if action in ("excluir", "delete", "remover"):
        task.status = "cancelled"
        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
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
async def dashboard_create_task(payload: CreateTaskPayload, db: AsyncSession = Depends(get_db)) -> dict:
    title = (payload.title or "").strip()
    if not title:
        return {"status": "error", "message": "title is required"}
    project = (payload.project or "").strip()
    full_title = f"{project} | {title}" if project else title
    if hasattr(task_manager, "canonicalize_task_title"):
        full_title = task_manager.canonicalize_task_title(full_title)
    deadline = None
    if payload.date:
        try:
            deadline = datetime.fromisoformat(payload.date)
        except ValueError:
            pass
    new_task = Task(title=full_title, origin="dashboard", status="pending", priority=payload.priority, deadline=deadline, category="work")
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
async def dashboard_task_edit(payload: TaskEditPayload, db: AsyncSession = Depends(get_db)) -> dict:
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
        if hasattr(task_manager, "canonicalize_task_title"):
            task.title = task_manager.canonicalize_task_title(full_title)
        else:
            task.title = full_title
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
    junk_result = await db.execute(
        select(AgendaBlock).where((AgendaBlock.source != "gcal") | (AgendaBlock.source == None))
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


@router.get("/night-summary")
async def night_summary(db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.time_utils import now_brt
    from datetime import timedelta, date, time

    agora = now_brt()
    hoje = agora.date()

    # Tasks concluídas hoje — usa completed_at (campo existente no modelo)
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
                pass

    # Tasks pendentes — dois .where() separados (& bitwise não funciona em SQLAlchemy)
    result2 = await db.execute(
        select(Task)
        .where(Task.status.in_(["pending", "in_progress", "active"]))
        .where(Task.category.not_like("personal%"))
    )
    pendentes = result2.scalars().all()

    # Amanhã
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
                    # project não existe no modelo — extrai do título "PROJ | task"
                    title = t.title or ''
                    if ' | ' in title:
                        parts = title.split(' | ', 1)
                        name = f"{parts[0]} | {parts[1]}"
                    else:
                        name = title
                    amanha_tasks.append(name)
            except Exception:
                pass

    summary = f"✅ {len(done_today)} task{'s' if len(done_today) != 1 else ''} concluída{'s' if len(done_today) != 1 else ''} hoje<br>"
    summary += f"⏳ {len(pendentes)} pendente{'s' if len(pendentes) != 1 else ''}"

    tomorrow = ""
    if amanha_tasks:
        tomorrow = "📋 amanhã:<br>" + "<br>".join(["• " + n for n in amanha_tasks[:5]])
    else:
        tomorrow = "✨ nada urgente amanhã"

    return {"summary": summary, "tomorrow": tomorrow}


# ── Jira integration ────────────────────────────────────────────────────────

import base64
import httpx


def _jira_auth_headers() -> dict:
    token = base64.b64encode(
        f"{settings.jira_email}:{settings.jira_api_token}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _jira_configured() -> bool:
    return bool(settings.jira_base_url and settings.jira_email and settings.jira_api_token)


_JIRA_PRIORITY_MAP = {"Lowest": 1, "Low": 2, "Medium": 3, "High": 4, "Highest": 5}

_JIRA_PROJECT_NAMES: dict[str, str] = {
    "SOM": "Squad Operação Marcom",
}


def _jira_project_name(key: str) -> str:
    prefix = key.split("-")[0] if "-" in key else key
    return _JIRA_PROJECT_NAMES.get(prefix, prefix)


def _jira_issue_to_dict(issue: dict, linked_keys: set[str]) -> dict:
    fields = issue.get("fields", {})
    key = issue.get("key", "")
    due = fields.get("duedate")
    priority_name = (fields.get("priority") or {}).get("name", "Medium")
    description_raw = fields.get("description") or {}
    # Extract plain text from Atlassian Document Format if present
    desc_text = ""
    if isinstance(description_raw, dict):
        try:
            for block in description_raw.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        desc_text += inline.get("text", "")
        except Exception:
            pass
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


@router.get("/jira/issues")
async def jira_list_issues(db: AsyncSession = Depends(get_db)) -> dict:
    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}

    # Buscar jira keys já linkadas (origin_ref onde origin == 'jira')
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
        return {"status": "error", "message": str(e)}

    issues = [_jira_issue_to_dict(i, linked_keys) for i in data.get("issues", [])]
    return {"status": "ok", "issues": issues, "total": len(issues)}


class JiraLinkPayload(BaseModel):
    task_id: str
    jira_key: str


@router.post("/jira/link")
async def jira_link_task(payload: JiraLinkPayload, db: AsyncSession = Depends(get_db)) -> dict:
    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}
    try:
        task_uuid = UUID(payload.task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}

    url = f"{settings.jira_base_url.rstrip('/')}/rest/api/2/issue/{payload.jira_key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_jira_auth_headers())
            resp.raise_for_status()
            issue = resp.json()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    fields = issue.get("fields", {})
    updated_fields = []

    task.origin_ref = payload.jira_key
    task.origin = "jira"
    updated_fields.append("jira_key")

    if not task.deadline and fields.get("duedate"):
        try:
            task.deadline = datetime.fromisoformat(fields["duedate"])
            updated_fields.append("deadline")
        except Exception:
            pass

    description_raw = fields.get("description") or {}
    desc_text = ""
    if isinstance(description_raw, dict):
        try:
            for block in description_raw.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        desc_text += inline.get("text", "")
        except Exception:
            pass
    elif isinstance(description_raw, str):
        desc_text = description_raw

    if desc_text.strip():
        notes = list(task.notes_json or [])
        notes.append({
            "text": f"[Jira] {desc_text[:500]}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        task.notes_json = notes
        updated_fields.append("notes")

    await db.commit()
    return {"status": "ok", "updated_fields": updated_fields}


class JiraImportPayload(BaseModel):
    keys: list[str]


@router.post("/jira/import")
async def jira_import_issues(payload: JiraImportPayload, db: AsyncSession = Depends(get_db)) -> dict:
    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}
    if not payload.keys:
        return {"status": "error", "message": "no keys provided"}

    imported_tasks = []
    for key in payload.keys:
        url = f"{settings.jira_base_url.rstrip('/')}/rest/api/2/issue/{key}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    url,
                    headers=_jira_auth_headers(),
                    params={"fields": "summary,status,duedate,priority,description"},
                )
                resp.raise_for_status()
                issue = resp.json()
        except Exception as e:
            imported_tasks.append({"key": key, "status": "error", "message": str(e)})
            continue

        fields = issue.get("fields", {})
        project_name = _jira_project_name(key)
        summary = fields.get("summary", key)
        full_title = f"{project_name} | {summary}"

        deadline = None
        if fields.get("duedate"):
            try:
                deadline = datetime.fromisoformat(fields["duedate"])
            except Exception:
                pass

        priority_name = (fields.get("priority") or {}).get("name", "Medium")
        priority_int = _JIRA_PRIORITY_MAP.get(priority_name, 3)

        description_raw = fields.get("description") or {}
        desc_text = ""
        if isinstance(description_raw, dict):
            try:
                for block in description_raw.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            desc_text += inline.get("text", "")
            except Exception:
                pass
        elif isinstance(description_raw, str):
            desc_text = description_raw

        notes_json = []
        if desc_text.strip():
            notes_json.append({
                "text": f"[Jira] {desc_text[:500]}",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })

        new_task = Task(
            title=full_title,
            origin="jira",
            origin_ref=key,
            status="pending",
            priority=priority_int,
            deadline=deadline,
            category="work",
            estimated_minutes=120,
            notes_json=notes_json if notes_json else None,
        )
        db.add(new_task)
        try:
            await db.flush()
            imported_tasks.append({
                "key": key,
                "status": "imported",
                "id": str(new_task.id),
                "title": full_title,
            })
        except Exception as e:
            await db.rollback()
            imported_tasks.append({"key": key, "status": "error", "message": str(e)})
            continue

    await db.commit()
    imported_count = sum(1 for t in imported_tasks if t.get("status") == "imported")
    return {"imported": imported_count, "tasks": imported_tasks}


# ── Unified input + chat history ────────────────────────────────────────────

@router.post("/input")
async def alfred_unified_input(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """Unified input: AI classifies intent and executes action."""
    from app.models import ChatMessage
    text = (body.get("text") or "").strip()
    if not text:
        return {"type": "error", "message": "texto vazio"}

    # Fetch last 10 messages for context
    try:
        hist_result = await db.execute(
            select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(10)
        )
        history = list(reversed(hist_result.scalars().all()))
    except Exception:
        history = []

    # Current tasks for context
    tasks_result = await db.execute(
        select(Task).where(Task.status.in_(("active", "pending", "in_progress")))
        .order_by(Task.deadline.asc().nulls_last()).limit(20)
    )
    current_tasks = tasks_result.scalars().all()
    tasks_context = "\n".join([f"- {t.title} (id:{t.id}, deadline:{t.deadline})" for t in current_tasks])

    history_text = "\n".join([
        f"{'Usuário' if m.role == 'user' else 'Alfred'}: {m.content}" for m in history
    ]) if history else ""

    today = _today_brt()
    prompt = f"""Você é o Alfred, assistente de produtividade pessoal. Classifique a intenção do usuário.

Tarefas ativas:
{tasks_context or '(nenhuma)'}

Histórico recente:
{history_text or '(nenhum)'}

Data atual: {today.isoformat()}

Entrada do usuário: "{text}"

Responda APENAS com JSON válido (sem markdown):
{{
  "intent": "create_task" | "update_task" | "complete_task" | "create_dump" | "query" | "unclear",
  "task_title": "título da tarefa se criar",
  "project": "nome do projeto se detectado",
  "deadline": "YYYY-MM-DD se mencionado ou null",
  "target_task_id": "uuid da task existente se update/complete",
  "dump_text": "texto se for dump/anotação",
  "message": "resposta para o usuário"
}}"""

    result: dict = {"type": "error", "message": "Erro interno"}
    intent = "unclear"

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model=settings.model_fast if hasattr(settings, 'model_fast') else "claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = msg.content[0].text.strip()
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        parsed = json.loads(response_text.strip())
        intent = parsed.get("intent", "unclear")

        if intent == "create_task":
            title = parsed.get("task_title") or text
            project = parsed.get("project") or ""
            full_title = f"{project} | {title}" if project else title
            deadline = None
            if parsed.get("deadline"):
                try:
                    deadline = datetime.fromisoformat(parsed["deadline"] + "T18:00:00")
                except Exception:
                    pass
            new_task = Task(
                title=full_title, origin="alfred_input", status="active",
                estimated_minutes=120, deadline=deadline, category="work", task_type="task",
                checklist_json=[], notes_json=[],
            )
            db.add(new_task)
            await db.commit()
            await db.refresh(new_task)
            result = {"type": "task_created", "id": str(new_task.id), "title": full_title,
                      "deadline": deadline.isoformat() if deadline else None,
                      "message": f"Tarefa criada: {full_title}"}

        elif intent == "complete_task":
            task_id = parsed.get("target_task_id")
            if task_id:
                try:
                    _r = await db.execute(select(Task).where(Task.id == UUID(task_id)))
                    _t = _r.scalar_one_or_none()
                    if _t:
                        _t.status = "done"
                        _t.completed_at = datetime.now(timezone.utc)
                        from sqlalchemy import delete as sa_delete
                        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == _t.id))
                        await db.commit()
                        result = {"type": "task_completed", "title": _t.title,
                                  "message": f"✅ Concluída: {_t.title}"}
                    else:
                        result = {"type": "clarification", "message": "Tarefa não encontrada."}
                except Exception:
                    result = {"type": "clarification", "message": "Qual tarefa você quer concluir?"}
            else:
                result = {"type": "clarification", "message": parsed.get("message", "Qual tarefa concluir?")}

        elif intent == "create_dump":
            dump_text = parsed.get("dump_text") or text
            new_dump = DumpItem(raw_text=dump_text, rewritten_title=dump_text[:100],
                                status="categorized", source="alfred_input", category="anotacao")
            db.add(new_dump)
            await db.commit()
            result = {"type": "dump_saved", "text": dump_text, "message": f"Anotado: {dump_text[:60]}"}

        elif intent == "update_task":
            task_id = parsed.get("target_task_id")
            if task_id:
                try:
                    _r = await db.execute(select(Task).where(Task.id == UUID(task_id)))
                    _t = _r.scalar_one_or_none()
                    if _t:
                        if parsed.get("deadline"):
                            try:
                                _t.deadline = datetime.fromisoformat(parsed["deadline"] + "T18:00:00")
                            except Exception:
                                pass
                        await db.commit()
                        result = {"type": "task_updated", "title": _t.title,
                                  "message": f"Atualizado: {_t.title}"}
                    else:
                        result = {"type": "clarification", "message": "Tarefa não encontrada."}
                except Exception:
                    result = {"type": "clarification", "message": parsed.get("message", "Qual tarefa alterar?")}
            else:
                result = {"type": "clarification", "message": parsed.get("message", "Qual tarefa alterar?")}

        elif intent == "query":
            result = {"type": "query_response", "message": parsed.get("message", "Consulta processada.")}

        else:
            result = {"type": "clarification", "message": parsed.get("message", "Não entendi. Tente: 'criar tarefa X' ou 'anotar Y'")}

    except Exception as e:
        result = {"type": "error", "message": f"Erro: {str(e)}"}

    # Save to chat history
    try:
        from app.models import ChatMessage as _CM
        db.add(_CM(role="user", content=text))
        db.add(_CM(role="assistant", content=result.get("message", ""), intent=intent, result_data=result))
        await db.commit()
    except Exception:
        pass

    return result


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

