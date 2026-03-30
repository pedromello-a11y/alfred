from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgendaBlock, DumpItem, PlayerStat, Streak, Task
from app.services.dashboard_projection import (
    get_active_queue,
    get_dump_library,
    get_focus_board,
    get_horizon_board,
)
from app.services.dump_manager import update_dump_item
from app.services.task_manager import update_task_status

router = APIRouter(prefix="/dashboard")


def _priority_label(p: int | None) -> str:
    if p is None:
        return "média"
    if p <= 2:
        return "alta"
    if p == 3:
        return "média"
    return "baixa"


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


def _task_dto(item: dict, is_first: bool = False) -> dict:
    priority = item.get("priority", "md")
    return {
        "id": item.get("id"),
        "name": item.get("title", ""),
        "badge": item.get("priorityLabel", "média"),
        "priority": priority,
        "cls": "cur" if is_first else ("hi" if priority == "hi" else ""),
    }


def _agenda_weekday_payload(timeline: list[dict]) -> list[dict]:
    if not timeline:
        return []
    today_dow = date.today().weekday()
    return [{
        "day": today_dow,
        "events": [
            {
                "title": block.get("title", ""),
                "time": block.get("start", ""),
                "end": block.get("end", ""),
                "type": block.get("type", "focus"),
            }
            for block in timeline
        ],
    }]


@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)):
    focus_board = await get_focus_board(db)
    horizon_board = await get_horizon_board(db)
    active_queue = await get_active_queue(db)
    dump_library = await get_dump_library(db)

    hoje = focus_board.get("todayTasks", [])
    hoje_ids = {item.get("id") for item in hoje}
    backlog = [item for item in active_queue if item.get("id") not in hoje_ids]
    if not hoje:
        hoje = active_queue[:3]
        backlog = active_queue[3:]

    current_block = focus_board.get("currentBlock")
    next_block = focus_board.get("nextBlock")
    focus_task = hoje[0] if hoje else None
    next_task = hoje[1] if len(hoje) > 1 else (backlog[0] if backlog else None)

    focus = {
        "title": (current_block or {}).get("title") or (focus_task or {}).get("title") or "nenhuma tarefa ativa",
        "project": ((focus_task or {}).get("title", "").split("|")[0].strip()) if focus_task else "",
        "estimate": (focus_task or {}).get("estimate") or "?",
        "deadline": (focus_task or {}).get("deadline") or "sem prazo",
        "priority": (focus_task or {}).get("priorityLabel") or "média",
    }

    next_info = {
        "title": (next_block or {}).get("title") or (next_task or {}).get("title") or "",
        "note": (next_block or {}).get("notes") or "",
    }

    xp_q = await db.execute(select(PlayerStat).where(PlayerStat.attribute == "craft"))
    stat = xp_q.scalar_one_or_none()
    level = stat.level if stat else 1
    xp_current = stat.xp if stat else 0
    xp_next_level = level * 1000
    xp_percent = min(int((xp_current / xp_next_level) * 100), 100) if xp_next_level else 0

    streak_q = await db.execute(select(Streak).order_by(Streak.streak_date.desc()).limit(1))
    latest_streak = streak_q.scalar_one_or_none()
    streak_count = latest_streak.streak_count if latest_streak else 0

    return {
        "focus": focus,
        "next": next_info,
        "tasks": {
            "hoje": [_task_dto(item, i == 0) for i, item in enumerate(hoje)],
            "backlog": [_task_dto(item) for item in backlog],
        },
        "agenda": _agenda_weekday_payload(focus_board.get("timeline", [])),
        "xp": {
            "level": level,
            "current": xp_current,
            "percent": xp_percent,
            "streak": streak_count,
        },
        "focusBoard": focus_board,
        "horizonBoard": horizon_board,
        "activeQueue": active_queue,
        "dumpLibrary": dump_library,
    }


@router.get("/focus")
async def dashboard_focus(db: AsyncSession = Depends(get_db)):
    return await get_focus_board(db)


@router.get("/horizon")
async def dashboard_horizon(db: AsyncSession = Depends(get_db)):
    return await get_horizon_board(db)


@router.get("/dumps")
async def dashboard_dumps(db: AsyncSession = Depends(get_db)):
    return await get_dump_library(db)


class ActionRequest(BaseModel):
    task_id: str
    action: Literal["concluida", "nota", "data", "excluir"]
    note: Optional[str] = None
    date: Optional[str] = None


@router.post("/action")
async def dashboard_action(body: ActionRequest, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(body.task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="task_id inválido")

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    if body.action == "concluida":
        await update_task_status(task, "done", db, note=body.note or None)
    elif body.action == "nota":
        if body.note:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            task.notes = f"{task.notes or ''}\n[{ts}] {body.note}".strip()
    elif body.action == "data":
        if body.date:
            try:
                task.deadline = datetime.fromisoformat(body.date).replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de data inválido")
    elif body.action == "excluir":
        await db.delete(task)

    await db.commit()
    return {"status": "ok", "task_id": body.task_id, "action": body.action}


class AgendaBlockRequest(BaseModel):
    title: str
    start_at: str
    end_at: str
    block_type: Literal["focus", "meeting", "break", "admin", "personal"] = "focus"
    source: str | None = "manual"
    notes: str | None = None
    linked_task_id: str | None = None


@router.post("/agenda-blocks")
async def create_agenda_block(body: AgendaBlockRequest, db: AsyncSession = Depends(get_db)):
    try:
        start_at = datetime.fromisoformat(body.start_at)
        end_at = datetime.fromisoformat(body.end_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datas inválidas")

    linked_task_id = None
    if body.linked_task_id:
        try:
            linked_task_id = UUID(body.linked_task_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="linked_task_id inválido")

    block = AgendaBlock(
        title=body.title,
        start_at=start_at,
        end_at=end_at,
        block_type=body.block_type,
        source=body.source,
        notes=body.notes,
        linked_task_id=linked_task_id,
    )
    db.add(block)
    await db.commit()
    await db.refresh(block)

    return {
        "status": "ok",
        "agenda_block": {
            "id": str(block.id),
            "title": block.title,
            "start_at": block.start_at.isoformat(),
            "end_at": block.end_at.isoformat(),
            "block_type": block.block_type,
        },
    }


class DumpUpdateRequest(BaseModel):
    dump_id: str
    category: str | None = None
    subcategory: str | None = None
    status: Literal["categorized", "unknown", "reviewed"] | None = None
    rewritten_title: str | None = None
    summary: str | None = None


@router.post("/dump-action")
async def dashboard_dump_action(body: DumpUpdateRequest, db: AsyncSession = Depends(get_db)):
    try:
        dump_uuid = UUID(body.dump_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="dump_id inválido")

    item = await update_dump_item(
        dump_uuid,
        db,
        category=body.category,
        subcategory=body.subcategory,
        status=body.status,
        rewritten_title=body.rewritten_title,
        summary=body.summary,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Dump não encontrado")

    return {"status": "ok", "dump_id": str(item.id)}