"""Router do dashboard — estado, foco, amanhã e ações."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.focus_snapshot import build_focus_snapshot
from app.services.tomorrow_board import build_tomorrow_board

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)) -> dict:
    focus = await build_focus_snapshot(db)
    tomorrow = await build_tomorrow_board(db)
    return {
        "focus": focus,
        "tomorrowBoard": tomorrow,
        "activeQueue": focus.get("active", []),
        "todayTasks": focus.get("dueToday", []),
        "overdueTasks": focus.get("overdue", []),
    }


@router.get("/focus")
async def dashboard_focus(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_focus_snapshot(db)


@router.get("/tomorrow")
async def dashboard_tomorrow(db: AsyncSession = Depends(get_db)) -> dict:
    return await build_tomorrow_board(db)
