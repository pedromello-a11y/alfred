from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Task, Streak, PlayerStat
from app.services import gcal_client
from app.services.task_manager import get_active_tasks, update_task_status

router = APIRouter(prefix="/dashboard")


# ── helpers ───────────────────────────────────────────────────────────────────

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


def _task_dto(t: Task, is_first: bool = False) -> dict:
    return {
        "id": str(t.id),
        "name": t.title,
        "badge": _priority_label(t.priority),
        "priority": _priority_dot(t.priority),
        "cls": "cur" if is_first else ("hi" if t.priority and t.priority <= 2 else ""),
    }


# ── GET /dashboard/state ──────────────────────────────────────────────────────

@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)):
    today = date.today()

    # Usa get_active_tasks — já aplica dedupe, canonicalização e filtra backlog/system
    active = list(await get_active_tasks(db))

    # "hoje" = tarefas planejadas para hoje ou in_progress; restante vai para backlog visual
    hoje = [t for t in active if t.status == "in_progress" or t.last_planned == today]
    backlog = [t for t in active if t not in hoje]

    # Se não há nada "hoje", mostra as primeiras 3 ativas como hoje
    if not hoje:
        hoje = active[:3]
        backlog = active[3:]

    focus_task = hoje[0] if hoje else None
    next_task = hoje[1] if len(hoje) > 1 else (backlog[0] if backlog else None)

    focus = {
        "title": focus_task.title if focus_task else "nenhuma tarefa ativa",
        "project": (focus_task.title.split("|")[0].strip()) if focus_task else "",
        "estimate": _estimate_label(focus_task.estimated_minutes) if focus_task else "?",
        "deadline": _deadline_label(focus_task.deadline) if focus_task else "sem prazo",
        "priority": _priority_label(focus_task.priority) if focus_task else "média",
    }

    next_info = {
        "title": next_task.title if next_task else "",
        "note": "",
    }

    # XP — usa atributo "craft" como proxy de nível geral
    xp_q = await db.execute(select(PlayerStat).where(PlayerStat.attribute == "craft"))
    stat = xp_q.scalar_one_or_none()
    level = stat.level if stat else 1
    xp_current = stat.xp if stat else 0
    xp_next_level = level * 1000
    xp_percent = min(int((xp_current / xp_next_level) * 100), 100) if xp_next_level else 0

    # Streak
    streak_q = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    latest_streak = streak_q.scalar_one_or_none()
    streak_count = latest_streak.streak_count if latest_streak else 0

    # Agenda — Google Calendar (hoje)
    agenda = []
    try:
        raw = await gcal_client.get_today_events()
        today_dow = today.weekday()  # 0=seg … 4=sex
        if 0 <= today_dow <= 4 and raw:
            def _hhmm(iso: str) -> str:
                try:
                    return datetime.fromisoformat(iso).strftime("%H:%M")
                except Exception:
                    return iso
            agenda_events = [
                {
                    "title": ev.get("title", ""),
                    "time": _hhmm(ev.get("start", "")),
                    "end": _hhmm(ev.get("end", "")),
                    "type": "meeting",
                }
                for ev in raw
            ]
            agenda = [{"day": today_dow, "events": agenda_events}]
    except Exception:
        agenda = []

    return {
        "focus": focus,
        "next": next_info,
        "tasks": {
            "hoje": [_task_dto(t, i == 0) for i, t in enumerate(hoje)],
            "backlog": [_task_dto(t) for t in backlog],
        },
        "agenda": agenda,
        "xp": {
            "level": level,
            "current": xp_current,
            "percent": xp_percent,
            "streak": streak_count,
        },
    }


# ── POST /dashboard/action ────────────────────────────────────────────────────

class ActionRequest(BaseModel):
    task_id: str
    action: Literal["concluida", "nota", "data", "excluir"]
    note: Optional[str] = None
    date: Optional[str] = None  # ISO e.g. "2026-04-01"


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
        # Delega para update_task_status — mesma lógica usada pelo message_handler
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
