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
        "deadlineHuman": _humanize_deadline(task.deadline),
        "deadlineType": dl_type,
        "status": task.status,
        "estimate": task.estimated_minutes or 120,
        "group": _get_task_group(task, today),
        "checklistTotal": len(checklist),
        "checklistDone": sum(1 for i in checklist if i.get("done")),
    }


async def _build_focus_v3(db: AsyncSession) -> tuple[dict, dict | None]:
    today = _today_brt()
    now_brt_dt = _now_brt()
    now_naive = now_brt_dt.replace(tzinfo=None) if now_brt_dt.tzinfo else now_brt_dt

    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
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
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.deadline >= datetime.combine(today, datetime.min.time()))
        .where(Task.deadline < datetime.combine(tomorrow, datetime.min.time()))
        .order_by(Task.deadline.asc())
    )
    tasks = result.scalars().all()
    items = []
    for task in tasks:
        project, task_name = _parse_project_task(task.title)
        checklist = getattr(task, "checklist_json", None) or []
        items.append({
            "id": str(task.id),
            "project": project,
            "taskName": task_name,
            "deadline": task.deadline.isoformat() if task.deadline else None,
            "deadlineHuman": _humanize_deadline(task.deadline),
            "deadlineType": getattr(task, "deadline_type", None) or "soft",
            "checklistTotal": len(checklist),
            "checklistDone": sum(1 for i in checklist if i.get("done")),
        })
    return items


async def _build_active_queue(db: AsyncSession) -> list[dict]:
    today = _today_brt()
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
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
    for block in blocks:
        if not block.start_at:
            continue
        dow = block.start_at.weekday()
        if dow > 4:
            continue
        days[dow].append({
            "title": block.title,
            "start": block.start_at.strftime("%H:%M"),
            "time": block.start_at.strftime("%H:%M"),
            "end": block.end_at.strftime("%H:%M") if block.end_at else "",
            "type": type_map.get(block.block_type or "focus", "focus"),
            "source": block.source or "manual",
        })

    deadlines = await _build_agenda_deadlines(db, monday, friday)

    return {
        "days": [{"day": d, "events": events} for d, events in days.items()],
        "suggestedBlocks": [],
        "pauses": [],
        "deadlines": deadlines,
        "weekStart": monday.isoformat(),
        "weekEnd": friday.isoformat(),
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

@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db), week_offset: int = 0) -> dict:
    focus, next_task = await _build_focus_v3(db)
    today_tasks = await _build_today_tasks(db)
    active_queue = await _build_active_queue(db)
    agenda_data = await _build_agenda_payload(db, week_offset)
    projects = await _get_project_names(db)

    state = {
        "focus": focus,
        "nextTask": next_task,
        "today": today_tasks,
        "activeQueue": active_queue,
        "agenda": agenda_data,
        "projects": projects,
        "riskAlert": None,
        # Backward compat fields for old HTML (can be removed after v3 HTML is deployed)
        "xp": await _build_xp_payload(db),
        "dumpLibrary": await _build_dump_library(db),
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

    await db.commit()
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

    new_task = Task(
        title=full_title,
        origin="dashboard",
        status="pending",
        estimated_minutes=body.get("estimate") or 120,
        deadline=deadline,
        category="work",
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


@router.get("/personal")
async def get_personal_items(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.category.like("personal%"))
        .order_by(Task.priority.asc().nulls_last(), Task.created_at.asc())
    )
    tasks = result.scalars().all()
    grouped: dict[str, list] = {}
    for task in tasks:
        cat = task.category or "personal"
        label = cat.replace("personal_", "").replace("_", " ").title() if "_" in cat else "Geral"
        grouped.setdefault(label, [])
        checklist = getattr(task, "checklist_json", None) or []
        grouped[label].append({
            "id": str(task.id),
            "title": task.title,
            "deadline": task.deadline.isoformat() if task.deadline else None,
            "deadlineHuman": _humanize_deadline(task.deadline),
            "checklistTotal": len(checklist),
            "checklistDone": sum(1 for i in checklist if i.get("done")),
        })
    return {"categories": [{"name": k, "items": v} for k, v in grouped.items()]}


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


@router.post("/dump")
async def create_quick_dump(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    new_dump = DumpItem(
        raw_text=text,
        rewritten_title=text[:100],
        status="categorized",
        source="dashboard",
    )
    db.add(new_dump)
    await db.commit()
    await db.refresh(new_dump)
    return {"status": "ok", "id": str(new_dump.id)}


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
