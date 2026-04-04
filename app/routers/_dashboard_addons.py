"""Novos endpoints do dashboard — parte 2."""
from __future__ import annotations

from datetime import date, datetime, timedelta, time as dt_time
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgendaBlock, PersonalItem, ScheduleBlock, Task
from app.routers.dashboard import router, _today_brt, _humanize_deadline, _parse_project_task


# ── ScheduleBlock helpers ───────────────────────────────────────────────────

def _sblock_to_dict(b: ScheduleBlock) -> dict:
    return {
        "id": str(b.id),
        "title": b.title,
        "block_type": b.block_type,
        "date": b.date.isoformat(),
        "start_time": b.start_time.strftime("%H:%M"),
        "end_time": b.end_time.strftime("%H:%M"),
        "is_fixed": b.is_fixed,
    }


@router.get("/schedule-blocks")
async def get_schedule_blocks(week: str | None = None, db: AsyncSession = Depends(get_db)) -> list:
    if week:
        try:
            week_start = date.fromisoformat(week)
        except Exception:
            today = _today_brt()
            week_start = today - timedelta(days=today.weekday())
    else:
        today = _today_brt()
        week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    result = await db.execute(
        select(ScheduleBlock)
        .where(ScheduleBlock.date >= week_start)
        .where(ScheduleBlock.date <= week_end)
        .order_by(ScheduleBlock.date.asc(), ScheduleBlock.start_time.asc())
    )
    return [_sblock_to_dict(b) for b in result.scalars().all()]


@router.post("/schedule-blocks")
async def create_schedule_block(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    title = (body.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    try:
        blk_date = date.fromisoformat(body["date"])
        start_t = dt_time.fromisoformat(body["start_time"])
        end_t = dt_time.fromisoformat(body["end_time"])
    except Exception as e:
        return {"error": str(e)}
    b = ScheduleBlock(
        title=title,
        block_type=body.get("block_type") or "other",
        date=blk_date,
        start_time=start_t,
        end_time=end_t,
        is_fixed=bool(body.get("is_fixed", True)),
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return _sblock_to_dict(b)


@router.put("/schedule-blocks/{block_id}")
async def update_schedule_block(block_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        bid = UUID(block_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(ScheduleBlock).where(ScheduleBlock.id == bid))
    b = result.scalar_one_or_none()
    if not b:
        return {"error": "not found"}
    if "title" in body:
        b.title = body["title"]
    if "block_type" in body:
        b.block_type = body["block_type"]
    if "date" in body:
        try:
            b.date = date.fromisoformat(body["date"])
        except Exception:
            pass
    if "start_time" in body:
        try:
            b.start_time = dt_time.fromisoformat(body["start_time"])
        except Exception:
            pass
    if "end_time" in body:
        try:
            b.end_time = dt_time.fromisoformat(body["end_time"])
        except Exception:
            pass
    await db.commit()
    await db.refresh(b)
    return _sblock_to_dict(b)


@router.delete("/schedule-blocks/{block_id}")
async def delete_schedule_block(block_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        bid = UUID(block_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(ScheduleBlock).where(ScheduleBlock.id == bid))
    b = result.scalar_one_or_none()
    if not b:
        return {"error": "not found"}
    await db.delete(b)
    await db.commit()
    return {"ok": True}


# ── PersonalItem endpoints ──────────────────────────────────────────────────

def _pitem_to_dict(p: PersonalItem) -> dict:
    return {
        "id": str(p.id),
        "title": p.title,
        "position": p.position,
        "done": p.done,
        "done_at": p.done_at.isoformat() if p.done_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/personal-items")
async def get_personal_items_v2(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(PersonalItem)
        .where(PersonalItem.user_id == "default")
        .order_by(PersonalItem.position.asc(), PersonalItem.created_at.asc())
    )
    return [_pitem_to_dict(p) for p in result.scalars().all()]


@router.post("/personal-items")
async def create_personal_item_v2(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    title = (body.get("title") or "").strip()
    if not title:
        return {"error": "title required"}
    from sqlalchemy import func as sqfunc
    r = await db.execute(
        select(sqfunc.max(PersonalItem.position)).where(PersonalItem.user_id == "default")
    )
    max_pos = r.scalar() or 0
    p = PersonalItem(title=title, position=max_pos + 1)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _pitem_to_dict(p)


@router.put("/personal-items/{item_id}")
async def update_personal_item_v2(item_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        iid = UUID(item_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(PersonalItem).where(PersonalItem.id == iid))
    p = result.scalar_one_or_none()
    if not p:
        return {"error": "not found"}
    if "title" in body:
        p.title = body["title"]
    if "done" in body:
        p.done = bool(body["done"])
        if p.done and not p.done_at:
            p.done_at = datetime.now()
        elif not p.done:
            p.done_at = None
    await db.commit()
    await db.refresh(p)
    return _pitem_to_dict(p)


@router.put("/personal-items/reorder")
async def reorder_personal_items_v2(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    items = body.get("items") or []
    for entry in items:
        try:
            iid = UUID(entry["id"])
        except Exception:
            continue
        result = await db.execute(select(PersonalItem).where(PersonalItem.id == iid))
        p = result.scalar_one_or_none()
        if p:
            p.position = int(entry.get("position", 0))
    await db.commit()
    return {"ok": True}


@router.delete("/personal-items/{item_id}")
async def delete_personal_item_v2(item_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        iid = UUID(item_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(PersonalItem).where(PersonalItem.id == iid))
    p = result.scalar_one_or_none()
    if not p:
        return {"error": "not found"}
    await db.delete(p)
    await db.commit()
    return {"ok": True}


# ── Agenda v2 (motor completo) ───────────────────────────────────────────────

def _minutes_between(t1: dt_time, t2: dt_time) -> int:
    return (t2.hour * 60 + t2.minute) - (t1.hour * 60 + t1.minute)


def _add_minutes_to_time(t: dt_time, mins: int) -> dt_time:
    total = t.hour * 60 + t.minute + mins
    total = max(0, min(total, 23 * 60 + 59))
    return dt_time(total // 60, total % 60)


def _factor_emoji(factor: float) -> str:
    if factor < 1.0:
        return "😰"
    if factor <= 1.3:
        return "😐"
    if factor <= 1.7:
        return "🙂"
    if factor <= 2.5:
        return "😌"
    return "😎"


async def _compute_agenda_v2(db: AsyncSession, week_start: date) -> dict:
    week_end = week_start + timedelta(days=6)
    today = _today_brt()

    tasks_result = await db.execute(
        select(Task)
        .where(Task.status.in_(("active", "pending", "in_progress")))
        .where(Task.category != "personal")
        .order_by(Task.deadline.asc().nulls_last(), Task.estimated_minutes.asc().nulls_last())
    )
    active_tasks = tasks_result.scalars().all()
    work_queue = [t for t in active_tasks if not (t.blocked or t.status == "on_holding")]

    sblocks_result = await db.execute(
        select(ScheduleBlock)
        .where(ScheduleBlock.date >= week_start)
        .where(ScheduleBlock.date <= week_end)
        .order_by(ScheduleBlock.date.asc(), ScheduleBlock.start_time.asc())
    )
    schedule_blocks_list = sblocks_result.scalars().all()

    gcal_start = datetime.combine(week_start, dt_time.min)
    gcal_end = datetime.combine(week_end, dt_time.max)
    gcal_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.source == "gcal")
        .where(AgendaBlock.start_at >= gcal_start)
        .where(AgendaBlock.start_at <= gcal_end)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    gcal_blocks = gcal_result.scalars().all()

    DAY_START = dt_time(8, 0)
    DAY_END = dt_time(18, 0)

    task_remaining: dict[str, int] = {}
    for t in work_queue:
        task_remaining[str(t.id)] = t.estimated_minutes or 30

    completions: dict[str, date | None] = {}
    days_output = []

    for day_offset in range(7):
        day_date = week_start + timedelta(days=day_offset)
        dow = day_date.weekday()
        is_weekend = dow >= 5
        day_name = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][dow]

        day_fixed = [b for b in schedule_blocks_list if b.date == day_date]
        day_gcal = [b for b in gcal_blocks if b.start_at.date() == day_date]

        occupied: list[tuple[dt_time, dt_time, str]] = []
        for b in day_fixed:
            occupied.append((b.start_time, b.end_time, "fixed"))
        for b in day_gcal:
            s = b.start_at.time().replace(second=0, microsecond=0)
            e = b.end_at.time().replace(second=0, microsecond=0)
            occupied.append((s, e, "gcal"))
        occupied.sort(key=lambda x: x[0])

        def get_free_slots(occ: list) -> list[tuple[dt_time, dt_time]]:
            slots = []
            cursor = DAY_START
            for os, oe, _ in sorted(occ, key=lambda x: x[0]):
                if os >= DAY_END:
                    break
                if cursor < os:
                    slots.append((cursor, min(os, DAY_END)))
                cursor = max(cursor, oe)
            if cursor < DAY_END:
                slots.append((cursor, DAY_END))
            return [(s, e) for s, e in slots if _minutes_between(s, e) > 0]

        free_slots = get_free_slots(occupied)
        total_available_mins = sum(_minutes_between(s, e) for s, e in free_slots)

        blocks_out: list[dict] = []

        for b in day_gcal:
            blocks_out.append({
                "type": "gcal",
                "title": b.title,
                "start": b.start_at.strftime("%H:%M"),
                "end": b.end_at.strftime("%H:%M"),
                "draggable": False,
            })

        for b in day_fixed:
            blocks_out.append({
                "type": "fixed",
                "title": b.title,
                "block_id": str(b.id),
                "block_type": b.block_type,
                "start": b.start_time.strftime("%H:%M"),
                "end": b.end_time.strftime("%H:%M"),
                "draggable": True,
            })

        estimated_hours = 0.0
        factor_day = 2.5

        if not is_weekend and work_queue:
            total_remaining = sum(task_remaining.values())
            remaining_workdays = max(1, sum(
                1 for d in range(7)
                if (week_start + timedelta(days=d)).weekday() < 5
                and (week_start + timedelta(days=d)) >= max(today, day_date)
            ))
            per_day_needed = total_remaining / remaining_workdays if remaining_workdays else 0
            if per_day_needed > 0 and total_available_mins > 0:
                factor = max(1.0, min(total_available_mins / per_day_needed, 2.5))
            else:
                factor = 2.5

            slot_list = list(free_slots)
            slot_idx = 0
            task_idx = 0
            allocated_today = 0
            auto_blocks: list[dict] = []

            while slot_idx < len(slot_list) and task_idx < len(work_queue):
                slot_start, slot_end = slot_list[slot_idx]
                slot_mins = _minutes_between(slot_start, slot_end)
                if slot_mins <= 0:
                    slot_idx += 1
                    continue

                task = work_queue[task_idx]
                tid = str(task.id)
                remaining = task_remaining.get(tid, 0)
                if remaining <= 0:
                    if tid not in completions:
                        completions[tid] = day_date
                    task_idx += 1
                    continue

                window_mins = min(int(remaining * factor), slot_mins, remaining)
                window_mins = max(window_mins, min(remaining, slot_mins))
                window_mins = min(window_mins, remaining, slot_mins)
                if window_mins <= 0:
                    slot_idx += 1
                    continue

                estimate_in_slot = int(window_mins / factor) if factor > 1.01 else window_mins
                estimate_in_slot = max(1, min(estimate_in_slot, remaining, window_mins))
                margin_mins = window_mins - estimate_in_slot

                block_end_t = _add_minutes_to_time(slot_start, window_mins)
                project, task_name = _parse_project_task(task.title)
                original_est = task.estimated_minutes or 30
                is_continuation = task_remaining.get(tid, original_est) < original_est

                auto_blocks.append({
                    "type": "auto",
                    "title": task_name or task.title,
                    "task_id": tid,
                    "project": project,
                    "start": slot_start.strftime("%H:%M"),
                    "end": block_end_t.strftime("%H:%M"),
                    "estimated_minutes": estimate_in_slot,
                    "window_minutes": window_mins,
                    "margin_minutes": margin_mins,
                    "is_continuation": is_continuation,
                    "task_total_hours": round(original_est / 60, 1),
                    "task_remaining_hours": round(remaining / 60, 1),
                    "task_completes_today": (remaining <= window_mins),
                    "draggable": False,
                    "deadline": task.deadline.isoformat() if task.deadline else None,
                    "deadline_human": _humanize_deadline(task.deadline),
                })

                allocated_today += window_mins
                task_remaining[tid] = max(0, remaining - window_mins)
                if task_remaining[tid] == 0:
                    completions[tid] = day_date
                    task_idx += 1

                remaining_slot_mins = slot_mins - window_mins
                if remaining_slot_mins > 0:
                    slot_list[slot_idx] = (_add_minutes_to_time(slot_start, window_mins), slot_end)
                else:
                    slot_idx += 1

            estimated_hours = round(allocated_today / 60, 1)
            factor_day = round(total_available_mins / max(allocated_today, 1), 2) if allocated_today > 0 else 2.5
            factor_day = min(factor_day, 2.5)
            blocks_out += auto_blocks

        blocks_out.sort(key=lambda b: b.get("start", "00:00"))

        days_output.append({
            "date": day_date.isoformat(),
            "day_name": day_name,
            "day_index": dow,
            "is_weekend": is_weekend,
            "available_hours": round(total_available_mins / 60, 1),
            "estimated_hours": estimated_hours,
            "factor": factor_day,
            "factor_emoji": _factor_emoji(factor_day),
            "blocks": blocks_out,
        })

    total_est = sum(t.estimated_minutes or 30 for t in work_queue)
    total_avail_mins = sum(
        d["available_hours"] * 60 for d in days_output if not d["is_weekend"]
    )
    overall_factor = round(total_avail_mins / max(total_est, 1), 2) if total_est > 0 else 2.5
    overall_factor = min(overall_factor, 2.5)

    completion_list = []
    for t in work_queue:
        tid = str(t.id)
        comp_date = completions.get(tid)
        if comp_date:
            _, tname = _parse_project_task(t.title)
            completion_list.append({
                "task_id": tid,
                "task": tname or t.title,
                "completes_on": comp_date.isoformat(),
                "completes_day": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][comp_date.weekday()],
            })

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "days": days_output,
        "summary": {
            "total_estimated": round(total_est / 60, 1),
            "total_available": round(total_avail_mins / 60, 1),
            "factor": overall_factor,
            "factor_emoji": _factor_emoji(overall_factor),
            "completions": completion_list,
        },
    }


@router.get("/agenda-v2")
async def get_agenda_v2(week: str | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    if week:
        try:
            ws = date.fromisoformat(week)
            week_start = ws - timedelta(days=ws.weekday())
        except Exception:
            today = _today_brt()
            week_start = today - timedelta(days=today.weekday())
    else:
        today = _today_brt()
        week_start = today - timedelta(days=today.weekday())
    return await _compute_agenda_v2(db, week_start)


# ── Seed demo ────────────────────────────────────────────────────────────────

@router.post("/seed-demo")
async def seed_demo_data(db: AsyncSession = Depends(get_db)) -> dict:
    today = _today_brt()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(weeks=1)
    next_wednesday = next_monday + timedelta(days=2)

    galaxy = Task(title="Galaxy 26", task_type="project", status="active", category="work")
    fire_proj = Task(title="FIRE 26", task_type="project", status="active", category="work")
    db.add(galaxy)
    db.add(fire_proj)
    await db.flush()

    filme = Task(title="Galaxy 26 | Filme Projetor", task_type="deliverable", status="active",
                 category="work", parent_id=galaxy.id)
    video = Task(title="Galaxy 26 | Video Abertura", task_type="deliverable", status="active",
                 category="work", parent_id=galaxy.id)
    db.add(filme)
    db.add(video)
    await db.flush()

    tasks_to_add = [
        Task(title="Galaxy 26 | Desdobrar em 25 saidas", task_type="subtask", status="active",
             category="work", parent_id=filme.id,
             deadline=datetime.combine(next_monday, dt_time(18, 0)), estimated_minutes=480),
        Task(title="Galaxy 26 | Preparar projetor", task_type="subtask", status="active",
             category="work", parent_id=filme.id,
             deadline=datetime.combine(next_monday, dt_time(18, 0)), estimated_minutes=120),
        Task(title="Galaxy 26 | Edicao final", task_type="subtask", status="backlog",
             category="work", parent_id=video.id, estimated_minutes=480),
        Task(title="FIRE 26 | Motion Kit: Logo + Letterings", task_type="task", status="active",
             category="work", parent_id=fire_proj.id,
             deadline=datetime.combine(next_wednesday, dt_time(18, 0)), estimated_minutes=300),
        Task(title="Subir metas no sistema", task_type="task", status="active", category="work",
             deadline=datetime.combine(today, dt_time(18, 0)), estimated_minutes=30),
        Task(title="Comprar projetores", task_type="task", status="backlog",
             category="work", estimated_minutes=120),
        Task(title="Cosmos II - Divoom", task_type="task", status="on_holding",
             category="work", blocked=True, blocked_reason="Esperando aprovacao cliente",
             blocked_until=date(2025, 4, 10), estimated_minutes=120),
    ]
    for t in tasks_to_add:
        db.add(t)

    for d in range(5):
        day = monday + timedelta(days=d)
        db.add(ScheduleBlock(title="Almoco", block_type="meal", date=day,
                             start_time=dt_time(12, 0), end_time=dt_time(13, 0)))
    db.add(ScheduleBlock(title="Corrida", block_type="exercise",
                         date=monday + timedelta(days=5),
                         start_time=dt_time(18, 0), end_time=dt_time(19, 0)))
    for d in [5, 6]:
        db.add(ScheduleBlock(title="Cachorro", block_type="pet",
                             date=monday + timedelta(days=d),
                             start_time=dt_time(8, 0), end_time=dt_time(8, 30)))

    for title, pos, done in [
        ("Comprar porta", 1, False),
        ("Marcar dermatologista", 2, False),
        ("Organizar armario", 3, False),
        ("Trocar lampada cozinha", 4, False),
        ("Pagar IPTU", 5, True),
    ]:
        p = PersonalItem(title=title, position=pos, done=done)
        if done:
            p.done_at = datetime.now()
        db.add(p)

    await db.commit()
    return {"ok": True, "message": "Seed data created successfully"}
