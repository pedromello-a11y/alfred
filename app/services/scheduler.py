"""Scheduler inteligente — recalcula agenda da semana inteira."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, Task
from app.services.time_utils import today_brt

logger = logging.getLogger(__name__)

WORK_START = 8 * 60     # 08:00 = 480min
WORK_END = 20 * 60      # 20:00 = 1200min
PREFER_BUFFER_START = 14 * 60  # 14:00
PREFER_BUFFER_END = 16 * 60    # 16:00


async def rebuild_week_schedule(
    db: AsyncSession, week_start: date, week_end: date
) -> list[AgendaBlock]:
    """Recalcula TODA a agenda da semana.

    Fluxo:
    1. Coleta constraints (gcal + pinned)
    2. Coleta tasks ativas ordenadas por deadline
    3. Calcula disponibilidade
    4. Apaga blocos suggested antigos
    5. Aloca tasks nos melhores slots
    6. Aplica buffer
    7. Preenche sobras com adiantamento
    """
    today = today_brt()

    # ═══ FASE 1: CONSTRAINTS ═══
    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time())

    constraints_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= week_start_dt)
        .where(AgendaBlock.start_at < week_end_dt)
        .where(AgendaBlock.status != "cancelled")
        .where(
            (AgendaBlock.source == "gcal") |
            (AgendaBlock.pinned == True)  # noqa: E712
        )
        .order_by(AgendaBlock.start_at.asc())
    )
    constraints = constraints_result.scalars().all()

    # Mapa de ocupação fixa: {date: [(start_min, end_min)]}
    fixed_occupation: dict[date, list[tuple[int, int]]] = {}
    for block in constraints:
        if not block.start_at or not block.end_at:
            continue
        d = block.start_at.date()
        s = block.start_at.hour * 60 + block.start_at.minute
        e = block.end_at.hour * 60 + block.end_at.minute
        fixed_occupation.setdefault(d, []).append((s, e))

    # ═══ FASE 2: FILA DE TASKS ═══
    all_tasks_result = await db.execute(
        select(Task)
        .where(Task.status.in_(("active", "pending", "in_progress")))
        .where(Task.task_type == "task")  # NUNCA project/deliverable
        .where(Task.deadline.isnot(None))
        .order_by(Task.deadline.asc(), Task.estimated_minutes.desc().nulls_last())
    )
    all_tasks = all_tasks_result.scalars().all()

    fila_urgente = [t for t in all_tasks if t.deadline and t.deadline.date() <= week_end]
    fila_futura = [t for t in all_tasks if t.deadline and t.deadline.date() > week_end]

    # Tasks sem deadline (para adiantamento)
    no_deadline_result = await db.execute(
        select(Task)
        .where(Task.status.in_(("active", "pending", "in_progress")))
        .where(Task.task_type == "task")
        .where(Task.deadline.is_(None))
        .order_by(Task.priority.asc().nulls_last(), Task.created_at.asc())
        .limit(10)
    )
    fila_sem_prazo = no_deadline_result.scalars().all()

    # ═══ FASE 3: DISPONIBILIDADE ═══
    availability: dict[date, list[tuple[int, int]]] = {}
    current_day = week_start
    while current_day <= week_end:
        if current_day.weekday() < 5 and current_day >= today:
            slots = _calc_free_slots(current_day, fixed_occupation)
            if slots:
                availability[current_day] = slots
        current_day += timedelta(days=1)

    # ═══ FASE 4: LIMPAR SUGESTÕES ANTIGAS ═══
    await db.execute(
        sa_delete(AgendaBlock).where(
            AgendaBlock.start_at >= week_start_dt,
            AgendaBlock.start_at < week_end_dt,
            AgendaBlock.source.in_(("alfred", "system")),
            AgendaBlock.pinned != True,  # noqa: E712
        )
    )

    # ═══ FASE 5: ALOCAR FILA URGENTE ═══
    created_blocks: list[AgendaBlock] = []
    allocated_task_ids: set = set()

    for task in fila_urgente:
        deadline_date = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        minutes = task.estimated_minutes or 120

        placements = _find_best_placement(availability, minutes, deadline_date, today)

        if placements:
            for (day, start_min, end_min) in placements:
                block = AgendaBlock(
                    title=task.title or "",
                    start_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=start_min),
                    end_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=end_min),
                    block_type="suggested",
                    source="alfred",
                    status="planned",
                    task_id=task.id,
                    pinned=False,
                )
                db.add(block)
                created_blocks.append(block)
                _consume_slot(availability, day, start_min, end_min)

            allocated_task_ids.add(task.id)

    # ═══ FASE 6: APLICAR BUFFER ═══
    _apply_buffer(availability)

    # ═══ FASE 7: ADIANTAMENTO ═══
    for day in sorted(availability.keys()):
        free_minutes = sum(e - s for s, e in availability.get(day, []))
        if free_minutes < 30:
            continue

        for task in fila_futura + list(fila_sem_prazo):
            if task.id in allocated_task_ids:
                continue

            minutes = task.estimated_minutes or 120
            for (s, e) in availability.get(day, []):
                if (e - s) >= minutes:
                    block = AgendaBlock(
                        title=task.title or "",
                        start_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=s),
                        end_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=s + minutes),
                        block_type="suggested",
                        source="alfred",
                        status="planned",
                        task_id=task.id,
                        pinned=False,
                    )
                    db.add(block)
                    created_blocks.append(block)
                    _consume_slot(availability, day, s, s + minutes)
                    allocated_task_ids.add(task.id)
                    break

            if task.id in allocated_task_ids:
                break  # 1 adiantamento por dia

    await db.commit()
    logger.info(
        f"Scheduler: {len(created_blocks)} blocos criados para {week_start}–{week_end}"
    )
    return created_blocks


def _calc_free_slots(
    day: date, fixed: dict[date, list[tuple[int, int]]]
) -> list[tuple[int, int]]:
    """Calcula slots livres no dia, descontando constraints fixas."""
    busy = sorted(fixed.get(day, []), key=lambda x: x[0])
    free = []
    cursor = WORK_START

    for bs, be in busy:
        if cursor < bs:
            free.append((cursor, bs))
        cursor = max(cursor, be)

    if cursor < WORK_END:
        free.append((cursor, WORK_END))

    return [(s, e) for s, e in free if (e - s) >= 5]


def _find_best_placement(
    availability: dict[date, list[tuple[int, int]]],
    minutes_needed: int,
    deadline: date,
    today: date,
) -> list[tuple[date, int, int]]:
    """Encontra os melhores slots para uma task (max 2 blocos).

    Score = (cabe em 1 bloco? +100) + (proximidade deadline * 10) + (tamanho * 0.1)
    """
    all_slots = []
    for day, slots in availability.items():
        if day > deadline:
            continue
        for start, end in slots:
            duration = end - start
            if duration < 5:
                continue

            fits = duration >= minutes_needed
            dist = max(1, (deadline - day).days)
            score = (100 if fits else 0) + (10.0 / dist) + (duration * 0.1)

            all_slots.append({
                "day": day, "start": start, "end": end,
                "duration": duration, "score": score,
            })

    if not all_slots:
        return []

    all_slots.sort(key=lambda s: -s["score"])
    best = all_slots[0]

    # 1 bloco contínuo
    if best["duration"] >= minutes_needed:
        return [(best["day"], best["start"], best["start"] + minutes_needed)]

    # Dividir em 2 (só se cada parte ≥ 30min)
    part1 = best["duration"]
    remaining = minutes_needed - part1

    if remaining < 30:
        # Sobra pequena demais — usa só o primeiro bloco
        return [(best["day"], best["start"], best["start"] + part1)]

    result = [(best["day"], best["start"], best["start"] + part1)]

    for slot in all_slots[1:]:
        if slot["duration"] >= remaining:
            result.append((slot["day"], slot["start"], slot["start"] + remaining))
            return result
        if slot["duration"] >= 30:
            use = slot["duration"]
            result.append((slot["day"], slot["start"], slot["start"] + use))
            return result

    return result


def _consume_slot(
    availability: dict[date, list[tuple[int, int]]],
    day: date,
    start: int,
    end: int,
) -> None:
    """Remove o tempo usado da disponibilidade."""
    if day not in availability:
        return

    new_slots = []
    for s, e in availability[day]:
        if end <= s or start >= e:
            new_slots.append((s, e))
        else:
            if s < start:
                new_slots.append((s, start))
            if end < e:
                new_slots.append((end, e))

    availability[day] = [(s, e) for s, e in new_slots if (e - s) >= 5]


def _apply_buffer(availability: dict[date, list[tuple[int, int]]]) -> None:
    """Remove tempo de buffer de cada dia (horário criativo protegido)."""
    for day in list(availability.keys()):
        slots = availability[day]
        total_free = sum(e - s for s, e in slots)
        total_day = WORK_END - WORK_START  # 720min
        used = total_day - total_free
        load = used / total_day if total_day > 0 else 1.0

        if load <= 0.6:
            buffer = 120
        elif load <= 0.8:
            buffer = 60
        else:
            buffer = 30

        remaining_buffer = buffer
        new_slots = []

        # Primeiro: remover do horário preferido (14h-16h)
        for s, e in sorted(slots, key=lambda x: x[0]):
            if remaining_buffer <= 0:
                new_slots.append((s, e))
                continue

            buf_start = max(s, PREFER_BUFFER_START)
            buf_end = min(e, PREFER_BUFFER_END)

            if buf_start < buf_end:
                can_remove = min(buf_end - buf_start, remaining_buffer)
                remaining_buffer -= can_remove
                if s < buf_start:
                    new_slots.append((s, buf_start))
                after = buf_start + can_remove
                if after < e:
                    new_slots.append((after, e))
            else:
                new_slots.append((s, e))

        # Se ainda sobrou buffer, remover do final do dia
        if remaining_buffer > 0:
            final_slots = []
            for s, e in sorted(new_slots, key=lambda x: -x[0]):
                if remaining_buffer <= 0:
                    final_slots.append((s, e))
                    continue
                duration = e - s
                if duration <= remaining_buffer:
                    remaining_buffer -= duration
                else:
                    final_slots.append((s, e - remaining_buffer))
                    remaining_buffer = 0
            new_slots = final_slots

        availability[day] = [(s, e) for s, e in new_slots if (e - s) >= 5]
