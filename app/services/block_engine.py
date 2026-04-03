"""Motor de blocos sugeridos — aloca tasks pendentes em slots livres da semana."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, Task
from app.services.time_utils import now_brt_naive, today_brt

# ── Configuração ───────────────────────────────────────────────────────────
WORK_START_H = 9        # hora de início do trabalho
WORK_END_H = 19         # hora de fim do trabalho
MIN_BLOCK_MIN = 30      # sessão mínima em minutos
MAX_BLOCK_MIN = 120     # sessão máxima por bloco sugerido
BREAK_MIN = 10          # pausa entre sessões


def _parse_project_task(title: str) -> tuple[str, str]:
    if "|" in (title or ""):
        p, t = title.split("|", 1)
        return p.strip(), t.strip()
    return "", (title or "").strip()


def _task_priority_key(task: Task, today: date) -> tuple:
    """Ordena: vencidas → prazo hoje → esta semana → próxima semana → sem prazo.
    Dentro de cada grupo, menor deadline e menor priority number primeiro."""
    if not task.deadline:
        return (4, 9999, task.priority or 5)
    try:
        dl = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        delta = (dl - today).days
    except Exception:
        return (4, 9999, task.priority or 5)
    if delta < 0:
        group = 0
    elif delta == 0:
        group = 1
    elif delta <= 6:
        group = 2
    elif delta <= 13:
        group = 3
    else:
        group = 4
    return (group, delta, task.priority or 5)


def _find_free_slots(
    day_date: date,
    existing_blocks: list[AgendaBlock],
    now_naive: datetime | None = None,
) -> list[tuple[datetime, datetime]]:
    """Retorna lista de (inicio, fim) dos slots livres no dia."""
    work_start = datetime.combine(day_date, time(WORK_START_H, 0))
    work_end = datetime.combine(day_date, time(WORK_END_H, 0))

    # Dias no passado: sem sugestões
    if now_naive and now_naive.date() > day_date:
        return []

    # Para hoje: começa a partir de agora (arredondado pra próxima meia hora)
    if now_naive and now_naive.date() == day_date:
        earliest = now_naive + timedelta(minutes=BREAK_MIN)
        # Arredonda pra próxima meia hora
        extra = (30 - earliest.minute % 30) % 30
        earliest = (earliest + timedelta(minutes=extra)).replace(second=0, microsecond=0)
        work_start = max(work_start, earliest)

    if work_start >= work_end:
        return []

    # Blocos existentes no dia, ordenados por início
    day_blocks = sorted(
        [
            b for b in existing_blocks
            if b.start_at and b.end_at
            and b.start_at.date() == day_date
            and b.status != "cancelled"
        ],
        key=lambda b: b.start_at,
    )

    free: list[tuple[datetime, datetime]] = []
    cursor = work_start

    for block in day_blocks:
        bs = block.start_at.replace(tzinfo=None) if block.start_at.tzinfo else block.start_at
        be = block.end_at.replace(tzinfo=None) if block.end_at.tzinfo else block.end_at
        bs = max(bs, work_start)
        be = min(be, work_end)
        if bs > cursor + timedelta(minutes=MIN_BLOCK_MIN):
            free.append((cursor, bs))
        cursor = max(cursor, be)

    if cursor < work_end - timedelta(minutes=MIN_BLOCK_MIN):
        free.append((cursor, work_end))

    return free


def _allocate(
    tasks: list[Task],
    days: list[tuple[int, date]],  # [(day_index, day_date), ...]
    existing_blocks: list[AgendaBlock],
    now_naive: datetime,
) -> list[dict]:
    """Aloca tasks nos slots livres. Retorna lista de blocos sugeridos."""
    # Minutos restantes por task (considera o total estimado)
    remaining: dict[str, int] = {
        str(t.id): (t.estimated_minutes or 120) for t in tasks
    }
    suggested: list[dict] = []

    for day_idx, day_date in days:
        free_slots = _find_free_slots(day_date, existing_blocks, now_naive)

        for slot_start, slot_end in free_slots:
            cursor = slot_start

            while cursor < slot_end:
                slot_remaining = int((slot_end - cursor).total_seconds() / 60)
                if slot_remaining < MIN_BLOCK_MIN:
                    break

                # Pega a próxima task com minutos restantes
                chosen = next(
                    (t for t in tasks if remaining.get(str(t.id), 0) > 0),
                    None,
                )
                if not chosen:
                    break

                duration = min(
                    remaining[str(chosen.id)],
                    MAX_BLOCK_MIN,
                    slot_remaining,
                )
                block_end = cursor + timedelta(minutes=duration)
                project, task_name = _parse_project_task(chosen.title)

                suggested.append({
                    "day": day_idx,
                    "title": task_name,
                    "start": cursor.strftime("%H:%M"),
                    "time": cursor.strftime("%H:%M"),
                    "end": block_end.strftime("%H:%M"),
                    "type": "suggested",
                    "source": "alfred",
                    "project": project,
                    "taskId": str(chosen.id),
                    "fullTitle": chosen.title,
                    "deadlineHuman": "",  # preenchido pelo caller se necessário
                })

                remaining[str(chosen.id)] = max(0, remaining[str(chosen.id)] - duration)
                cursor = block_end + timedelta(minutes=BREAK_MIN)

    return suggested


def _calc_risk(
    tasks: list[Task],
    suggested: list[dict],
    today: date,
    week_end: date,
) -> dict | None:
    """Calcula risco: horas necessárias vs disponíveis esta semana."""
    # Horas necessárias = tasks com deadline até fim da semana
    needed_min = 0
    for t in tasks:
        if not t.deadline:
            continue
        try:
            dl = t.deadline.date() if hasattr(t.deadline, "date") else t.deadline
        except Exception:
            continue
        if dl <= week_end:
            needed_min += t.estimated_minutes or 120

    if needed_min == 0:
        return None

    # Horas disponíveis = soma de todos os blocos sugeridos até fim da semana
    available_min = sum(
        int(
            (
                datetime.strptime(b["end"], "%H:%M") - datetime.strptime(b["start"], "%H:%M")
            ).total_seconds() / 60
        )
        for b in suggested
    )

    needed_h = round(needed_min / 60, 1)
    available_h = round(available_min / 60, 1)
    deficit = round(max(0, needed_h - available_h), 1)

    if deficit <= 0:
        return None

    suggestion = (
        f"Déficit de {deficit}h esta semana. "
        f"Considere adiar tarefas ou trabalhar além do horário padrão."
    )

    return {
        "totalHoursNeeded": needed_h,
        "totalHoursAvailable": available_h,
        "deficit": deficit,
        "suggestion": suggestion,
    }


async def build_suggested_blocks(
    db: AsyncSession,
    week_start: date,
    week_end: date,
) -> tuple[list[dict], dict | None]:
    """
    Entry point principal.
    Retorna (suggested_blocks, risk_alert).
    """
    today = today_brt()
    now_naive = now_brt_naive()

    # Tasks ativas de trabalho, ordenadas por prioridade
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.category != "personal")
        .limit(30)
    )
    raw_tasks = result.scalars().all()
    work_tasks = [t for t in raw_tasks if not (t.category or "").startswith("personal")]
    work_tasks.sort(key=lambda t: _task_priority_key(t, today))

    if not work_tasks:
        return [], None

    # Blocos existentes na semana (gcal + manuais)
    monday_dt = datetime.combine(week_start, datetime.min.time())
    friday_dt = datetime.combine(week_end, datetime.max.time().replace(microsecond=0))
    blocks_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= monday_dt)
        .where(AgendaBlock.start_at <= friday_dt)
        .where(AgendaBlock.status != "cancelled")
    )
    existing_blocks = list(blocks_result.scalars().all())

    # Dias da semana (seg=0 a sex=4)
    days: list[tuple[int, date]] = [
        (i, week_start + timedelta(days=i)) for i in range(5)
        if (week_start + timedelta(days=i)) <= week_end
    ]

    suggested = _allocate(work_tasks, days, existing_blocks, now_naive)
    risk = _calc_risk(work_tasks, suggested, today, week_end)

    return suggested, risk


def find_next_block_for_task(suggested: list[dict], task_id: str) -> str:
    """Retorna o horário de início do próximo bloco sugerido para uma task."""
    for block in suggested:
        if block.get("taskId") == task_id:
            return block["start"]
    return ""
