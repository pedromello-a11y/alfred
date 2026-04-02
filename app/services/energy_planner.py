"""Planejador de energia — distribui tasks em blocos por tipo de energia."""
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, Task
from app.services.time_utils import now_brt_naive, today_brt


class EnergyLevel(Enum):
    CREATIVE = "creative"    # morning pre-meeting
    MECHANICAL = "mechanical"  # post-meeting, afternoon
    LOW = "low"              # end of day


ENERGY_WINDOWS = [
    (time(7, 0), time(10, 0), EnergyLevel.CREATIVE),
    (time(10, 0), time(12, 0), EnergyLevel.MECHANICAL),
    (time(12, 0), time(14, 0), EnergyLevel.LOW),
    (time(14, 0), time(17, 0), EnergyLevel.MECHANICAL),
    (time(17, 0), time(21, 0), EnergyLevel.LOW),
]

TRANSITION_BUFFER_MINUTES = 15


def classify_time_slot(
    hour: int, minute: int = 0, has_meeting_before: bool = False
) -> EnergyLevel:
    t = time(hour, minute)
    if has_meeting_before and hour < 12:
        return EnergyLevel.MECHANICAL
    for start, end, energy in ENERGY_WINDOWS:
        if start <= t < end:
            return energy
    return EnergyLevel.LOW


def match_task_to_energy(task: Task) -> EnergyLevel:
    title = (task.title or "").lower()
    cat = (task.category or "").lower()

    creative_signals = (
        "edição", "edicao", "video", "vídeo", "design", "motion",
        "animação", "animacao", "render", "roteiro", "criativo", "storyboard",
    )
    if any(w in title for w in creative_signals):
        return EnergyLevel.CREATIVE

    mechanical_signals = (
        "email", "jira", "admin", "revisão", "revisao", "review",
        "reunião", "reuniao", "meeting", "planejamento", "relatório",
    )
    if any(w in title for w in mechanical_signals):
        return EnergyLevel.MECHANICAL

    if task.estimated_minutes and task.estimated_minutes < 15:
        return EnergyLevel.LOW
    if cat == "personal" and (task.effort_type == "quick" or (task.estimated_minutes or 30) < 30):
        return EnergyLevel.LOW

    return EnergyLevel.MECHANICAL


async def get_available_slots(db: AsyncSession) -> list[dict]:
    """Calculates free slots today after subtracting existing agenda blocks."""
    today = today_brt()
    now = now_brt_naive()
    day_start = datetime.combine(today, time(7, 0))
    day_end = datetime.combine(today, time(21, 0))

    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= day_start)
        .where(AgendaBlock.end_at <= day_end)
        .where(AgendaBlock.status == "planned")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = list(result.scalars().all())
    occupied = [(b.start_at, b.end_at) for b in blocks]

    slots: list[dict] = []
    cursor = max(day_start, now)

    for occ_start, occ_end in occupied:
        if cursor < occ_start:
            buffer_end = occ_start - timedelta(minutes=TRANSITION_BUFFER_MINUTES)
            if cursor < buffer_end:
                energy = classify_time_slot(cursor.hour, cursor.minute)
                slots.append({
                    "start": cursor,
                    "end": buffer_end,
                    "energy": energy,
                    "duration_min": int((buffer_end - cursor).total_seconds() / 60),
                })
        cursor = max(cursor, occ_end + timedelta(minutes=TRANSITION_BUFFER_MINUTES))

    if cursor < day_end:
        energy = classify_time_slot(cursor.hour, cursor.minute)
        slots.append({
            "start": cursor,
            "end": day_end,
            "energy": energy,
            "duration_min": int((day_end - cursor).total_seconds() / 60),
        })

    return slots


def distribute_tasks_by_energy(
    tasks: Sequence[Task],
    slots: list[dict],
) -> list[dict]:
    """Distributes tasks into time slots by energy compatibility."""
    assignments: list[dict] = []
    remaining_tasks = list(tasks)
    remaining_slots = [dict(s) for s in slots]

    for energy_level in (EnergyLevel.CREATIVE, EnergyLevel.MECHANICAL, EnergyLevel.LOW):
        matching_tasks = [t for t in remaining_tasks if match_task_to_energy(t) == energy_level]
        matching_slots = [s for s in remaining_slots if s["energy"] == energy_level]

        for task in list(matching_tasks):
            est = task.estimated_minutes or 30
            for slot in matching_slots:
                if slot["duration_min"] >= est:
                    assignments.append({
                        "task_id": str(task.id),
                        "task_title": task.title,
                        "slot_start": slot["start"],
                        "slot_end": slot["start"] + timedelta(minutes=est),
                        "energy": energy_level.value,
                    })
                    new_start = slot["start"] + timedelta(minutes=est + TRANSITION_BUFFER_MINUTES)
                    slot["start"] = new_start
                    slot["duration_min"] = max(
                        0, int((slot["end"] - new_start).total_seconds() / 60)
                    )
                    remaining_tasks.remove(task)
                    break

    # Tasks with no ideal slot → place wherever they fit
    for task in list(remaining_tasks):
        est = task.estimated_minutes or 30
        for slot in remaining_slots:
            if slot["duration_min"] >= est:
                energy_val = (
                    slot["energy"].value
                    if isinstance(slot["energy"], EnergyLevel)
                    else slot["energy"]
                )
                assignments.append({
                    "task_id": str(task.id),
                    "task_title": task.title,
                    "slot_start": slot["start"],
                    "slot_end": slot["start"] + timedelta(minutes=est),
                    "energy": energy_val,
                })
                new_start = slot["start"] + timedelta(minutes=est + TRANSITION_BUFFER_MINUTES)
                slot["start"] = new_start
                slot["duration_min"] = max(
                    0, int((slot["end"] - new_start).total_seconds() / 60)
                )
                remaining_tasks.remove(task)
                break

    return assignments


async def compute_available_hours(db: AsyncSession) -> float:
    """Returns total available hours remaining today after agenda blocks."""
    slots = await get_available_slots(db)
    total_minutes = sum(s["duration_min"] for s in slots)
    return total_minutes / 60.0
