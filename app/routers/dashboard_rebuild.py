from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgendaBlock, Task
from app.services import task_manager
from app.services.time_utils import now_brt, today_brt
from app.services.tomorrow_board import build_tomorrow_board

router = APIRouter(prefix="/dashboard")


def _serialize_task(task: Task) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "deadline": task.deadline.strftime("%d/%m %H:%M") if task.deadline else "sem prazo",
        "category": task.category or "work",
    }


@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)):
    today = today_brt()
    active = list(await task_manager.get_active_tasks(db))
    due_today = [t for t in active if t.deadline and t.deadline.date() == today]
    overdue = [t for t in active if t.deadline and t.deadline.date() < today]
    now = now_brt().replace(tzinfo=None)

    current_block = None
    next_block = None
    result = await db.execute(select(AgendaBlock).order_by(AgendaBlock.start_at.asc()))
    for block in result.scalars().all():
        if block.start_at <= now < block.end_at:
            current_block = block
        elif block.start_at > now and next_block is None:
            next_block = block

    return {
        "nowLabel": now_brt().strftime("%H:%M BRT"),
        "currentBlock": {
            "title": current_block.title,
            "start": current_block.start_at.strftime("%H:%M"),
            "end": current_block.end_at.strftime("%H:%M"),
            "type": current_block.block_type,
        } if current_block else None,
        "nextBlock": {
            "title": next_block.title,
            "start": next_block.start_at.strftime("%H:%M"),
            "end": next_block.end_at.strftime("%H:%M"),
            "type": next_block.block_type,
        } if next_block else None,
        "todayTasks": [_serialize_task(t) for t in due_today[:5]],
        "overdueTasks": [_serialize_task(t) for t in overdue[:5]],
        "activeQueue": [_serialize_task(t) for t in active[:15]],
        "tomorrowBoard": await build_tomorrow_board(db),
    }


@router.get("/tomorrow")
async def dashboard_tomorrow(db: AsyncSession = Depends(get_db)):
    return await build_tomorrow_board(db)
