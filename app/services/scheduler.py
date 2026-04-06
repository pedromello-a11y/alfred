"""Scheduler inteligente — recalcula agenda da semana inteira."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ACTIVE_STATUSES,
    SCHEDULABLE_TASK_TYPES,
    WORK_START_MINUTES as WORK_START,
    WORK_END_MINUTES as WORK_END,
    BUFFER_PREFERRED_START as BUFFER_PREF_START,
    BUFFER_PREFERRED_END as BUFFER_PREF_END,
    DAY_GROSS_MINUTES,
    MICRO_BUFFER_MINUTES,
)
from app.models import AgendaBlock, Task
from app.services.time_utils import today_brt

logger = logging.getLogger(__name__)


async def rebuild_week_schedule(
    db: AsyncSession,
    week_start: date,
    week_end: date,
) -> list[AgendaBlock]:
    today = today_brt()

    # Nuclear cleanup: deletar TODOS blocos que referenciam non-tasks
    _non_task_ids = select(Task.id).where(Task.task_type.in_(["project", "deliverable"]))
    await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id.in_(_non_task_ids)))
    await db.flush()

    # ══════════════════════════════════════════════
    # FASE 1: CONSTRAINTS (gcal + pinned = imutáveis)
    # ══════════════════════════════════════════════
    ws_dt = datetime.combine(week_start, datetime.min.time())
    we_dt = datetime.combine(week_end + timedelta(days=1), datetime.min.time())

    constraints_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= ws_dt)
        .where(AgendaBlock.start_at < we_dt)
        .where(AgendaBlock.status != "cancelled")
        .where(
            (AgendaBlock.source == "gcal") |
            (AgendaBlock.pinned == True)  # noqa: E712
        )
    )
    constraints = constraints_result.scalars().all()

    # Mapa de ocupação fixa: {date: [(start_min, end_min), ...]}
    fixed: dict[date, list[tuple[int, int]]] = {}
    for b in constraints:
        if not b.start_at or not b.end_at:
            continue
        d = b.start_at.date()
        s = b.start_at.hour * 60 + b.start_at.minute
        e = b.end_at.hour * 60 + b.end_at.minute
        fixed.setdefault(d, []).append((s, e))

    # ══════════════════════════════════════════════
    # FASE 2: DISPONIBILIDADE BRUTA
    # ══════════════════════════════════════════════
    availability: dict[date, list[tuple[int, int]]] = {}
    cur = week_start
    while cur <= week_end:
        if cur.weekday() < 5 and cur >= today:
            slots = _free_slots(cur, fixed)
            if slots:
                availability[cur] = slots
        cur += timedelta(days=1)

    # ══════════════════════════════════════════════
    # FASE 3: APLICAR BUFFER ***ANTES*** DA ALOCAÇÃO
    # ══════════════════════════════════════════════
    for day in list(availability.keys()):
        _remove_buffer(availability, day, fixed)

    # ══════════════════════════════════════════════
    # FASE 4: FILA DE TASKS
    # ══════════════════════════════════════════════
    tasks_result = await db.execute(
        select(Task)
        .where(Task.status.in_(ACTIVE_STATUSES))
        .where(Task.task_type.in_(list(SCHEDULABLE_TASK_TYPES)))
        .where(Task.deadline.isnot(None))
        .order_by(Task.deadline.asc(), Task.estimated_minutes.desc().nulls_last())
    )
    all_tasks = tasks_result.scalars().all()

    # Separar urgente (deadline <= week_end) e futura
    fila_urgente = []
    fila_futura = []
    for t in all_tasks:
        try:
            dl = t.deadline.date() if hasattr(t.deadline, "date") else t.deadline
        except Exception:
            continue
        if dl <= week_end:
            fila_urgente.append(t)
        else:
            fila_futura.append(t)

    # OVERDUE: tasks com deadline < hoje → tratar deadline como HOJE
    def _effective_deadline(t: Task) -> date:
        try:
            dl = t.deadline.date() if hasattr(t.deadline, "date") else t.deadline
        except Exception:
            return week_end
        if dl < today:
            return today
        return dl

    fila_urgente.sort(key=lambda t: (_effective_deadline(t), -(t.estimated_minutes or 0)))

    # Tasks sem deadline (para adiantamento)
    no_dl_result = await db.execute(
        select(Task)
        .where(Task.status.in_(ACTIVE_STATUSES))
        .where(Task.task_type.in_(list(SCHEDULABLE_TASK_TYPES)))
        .where(Task.deadline.is_(None))
        .order_by(Task.created_at.asc())
        .limit(10)
    )
    fila_sem_prazo = no_dl_result.scalars().all()

    # ══════════════════════════════════════════════
    # FASE 5: LIMPAR BLOCOS ANTIGOS
    # ══════════════════════════════════════════════
    await db.execute(
        sa_delete(AgendaBlock).where(
            AgendaBlock.start_at >= ws_dt,
            AgendaBlock.start_at < we_dt,
            AgendaBlock.source.in_(("alfred", "system")),
            AgendaBlock.pinned != True,  # noqa: E712
        )
    )
    # Limpar blocos que referenciam deliverables/projects
    wrong_result = await db.execute(
        select(AgendaBlock.id)
        .join(Task, AgendaBlock.task_id == Task.id)
        .where(
            AgendaBlock.start_at >= ws_dt,
            AgendaBlock.start_at < we_dt,
            Task.task_type.in_(["project", "deliverable"]),
        )
    )
    wrong_ids = [r[0] for r in wrong_result.all()]
    if wrong_ids:
        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.id.in_(wrong_ids)))

    await db.flush()

    # ══════════════════════════════════════════════
    # FASE 6: ALOCAR FILA URGENTE
    # ══════════════════════════════════════════════
    created: list[AgendaBlock] = []
    allocated_ids: set[UUID] = set()

    for task in fila_urgente:
        try:
            original_dl = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        except Exception:
            original_dl = week_end
        is_overdue = original_dl < today
        dl = _effective_deadline(task)
        minutes = task.estimated_minutes or 120

        placements = _find_best_placement(availability, minutes, dl, today, is_overdue=is_overdue)
        if not placements:
            continue

        for (day, s, e) in placements:
            block = AgendaBlock(
                title=task.title or "",
                start_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=s),
                end_at=datetime.combine(day, datetime.min.time()) + timedelta(minutes=e),
                block_type="suggested",
                source="alfred",
                status="planned",
                task_id=task.id,
                pinned=False,
            )
            db.add(block)
            created.append(block)
            _consume_slot(availability, day, s, e)

        allocated_ids.add(task.id)

    # ══════════════════════════════════════════════
    # FASE 7: ADIANTAMENTO (se sobrar espaço)
    # ══════════════════════════════════════════════
    for day in sorted(availability.keys()):
        total_free = sum(e - s for s, e in availability.get(day, []))
        if total_free < 30:
            continue

        for task in fila_futura + list(fila_sem_prazo):
            if task.id in allocated_ids:
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
                    created.append(block)
                    _consume_slot(availability, day, s, s + minutes)
                    allocated_ids.add(task.id)
                    break
            if task.id in allocated_ids:
                break  # 1 adiantamento por dia

    await db.commit()
    logger.info(f"Scheduler: {len(created)} blocos, semana {week_start}→{week_end}")
    return created


# ─── Funções auxiliares ───────────────────────────


def _free_slots(day: date, fixed: dict) -> list[tuple[int, int]]:
    """Slots livres no dia (8h-20h menos constraints)."""
    busy = sorted(fixed.get(day, []), key=lambda x: x[0])
    free = []
    cursor = WORK_START
    for (bs, be) in busy:
        if cursor < bs:
            free.append((cursor, bs))
        cursor = max(cursor, be)
    if cursor < WORK_END:
        free.append((cursor, WORK_END))
    return free


def _remove_buffer(availability: dict, day: date, fixed: dict) -> None:
    """Remove buffer da disponibilidade ANTES da alocação.

    Buffer dinâmico baseado na carga do dia:
    - Carga ≤ 60%: 120min (2h)
    - Carga 60-80%: 60min (1h)
    - Carga > 80%: 30min

    Preferência: remover das 14h-16h, depois do final do dia.
    """
    if day not in availability:
        return

    total_day = WORK_END - WORK_START  # 720min
    busy_minutes = sum(e - s for s, e in fixed.get(day, []))
    load = busy_minutes / total_day if total_day > 0 else 0

    if load <= 0.6:
        buffer = 120
    elif load <= 0.8:
        buffer = 60
    else:
        buffer = 30

    remaining = buffer
    slots = availability[day]
    new_slots = []

    # Primeiro: tentar remover do horário preferido (14h-16h)
    for (s, e) in sorted(slots, key=lambda x: x[0]):
        if remaining <= 0:
            new_slots.append((s, e))
            continue

        overlap_start = max(s, BUFFER_PREF_START)
        overlap_end = min(e, BUFFER_PREF_END)

        if overlap_start < overlap_end:
            can_remove = min(overlap_end - overlap_start, remaining)
            remaining -= can_remove
            if s < overlap_start:
                new_slots.append((s, overlap_start))
            new_end = overlap_start + can_remove
            if new_end < e:
                new_slots.append((new_end, e))
        else:
            new_slots.append((s, e))

    # Se ainda sobrou buffer, remover do final do dia
    if remaining > 0:
        final = []
        for (s, e) in sorted(new_slots, key=lambda x: -x[0]):
            if remaining <= 0:
                final.append((s, e))
                continue
            dur = e - s
            if dur <= remaining:
                remaining -= dur
            else:
                final.append((s, e - remaining))
                remaining = 0
        new_slots = final

    availability[day] = [(s, e) for s, e in new_slots if (e - s) >= 5]


def _find_best_placement(
    availability: dict,
    minutes: int,
    deadline: date,
    today: date,
    is_overdue: bool = False,
) -> list[tuple[date, int, int]]:
    """Encontra melhor slot(s) para uma task.

    Score:
    - Cabe em 1 bloco: +100
    - Proximidade ao deadline: +10/distância
    - Tamanho do slot: +0.1*duração

    Nunca divide em 3+. Cada parte ≥ 30min.
    """
    all_slots = []
    for day, slots in availability.items():
        # Overdue: permite qualquer dia da semana (sem restrição de deadline)
        if not is_overdue and day > deadline:
            continue
        for (s, e) in slots:
            dur = e - s
            if dur < 5:
                continue
            dist = max(1, (deadline - day).days)
            fits = dur >= minutes
            score = (100 if fits else 0) + (10.0 / dist) + (dur * 0.1)
            all_slots.append({
                "day": day, "s": s, "e": e,
                "dur": dur, "score": score,
            })

    if not all_slots:
        return []

    all_slots.sort(key=lambda x: -x["score"])
    best = all_slots[0]

    # Cabe em 1 bloco
    if best["dur"] >= minutes:
        return [(best["day"], best["s"], best["s"] + minutes)]

    # Precisa dividir em 2
    part1 = min(best["dur"], minutes)
    remaining = minutes - part1

    if remaining < 30:
        return [(best["day"], best["s"], best["s"] + part1)]

    result = [(best["day"], best["s"], best["s"] + part1)]

    for slot in all_slots[1:]:
        if slot["dur"] >= remaining:
            result.append((slot["day"], slot["s"], slot["s"] + remaining))
            return result

    if len(all_slots) > 1:
        s2 = all_slots[1]
        use = min(s2["dur"], remaining)
        if use >= 30:
            result.append((s2["day"], s2["s"], s2["s"] + use))

    return result


def _consume_slot(availability: dict, day: date, start: int, end: int) -> None:
    """Remove tempo usado da disponibilidade."""
    if day not in availability:
        return
    new_slots = []
    for (s, e) in availability[day]:
        if end <= s or start >= e:
            new_slots.append((s, e))
        else:
            if s < start:
                new_slots.append((s, start))
            if end < e:
                new_slots.append((end, e))
    availability[day] = [(s, e) for s, e in new_slots if (e - s) >= 5]
