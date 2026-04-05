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
    """Aloca tasks nos slots livres. Retorna lista de blocos sugeridos.
    Tasks com estimated_minutes maior que o disponível num dia são divididas
    em blocos consecutivos com indicador de parte (1/2, 2/2, etc.)."""
    total_minutes: dict[str, int] = {
        str(t.id): (t.estimated_minutes or 120) for t in tasks
    }
    # Minutos restantes por task (decresce conforme alocamos)
    remaining: dict[str, int] = dict(total_minutes)
    # Contador de partes já geradas por task
    parts_count: dict[str, int] = {str(t.id): 0 for t in tasks}
    # Blocos pendentes para calcular part labels (índice na lista suggested)
    task_block_indices: dict[str, list[int]] = {str(t.id): [] for t in tasks}
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

                tid = str(chosen.id)
                duration = min(
                    remaining[tid],
                    MAX_BLOCK_MIN,
                    slot_remaining,
                )
                block_end = cursor + timedelta(minutes=duration)
                project, task_name = _parse_project_task(chosen.title)

                parts_count[tid] += 1
                block_idx = len(suggested)
                task_block_indices[tid].append(block_idx)

                suggested.append({
                    "day": day_idx,
                    "title": task_name,
                    "start": cursor.strftime("%H:%M"),
                    "time": cursor.strftime("%H:%M"),
                    "end": block_end.strftime("%H:%M"),
                    "type": "suggested",
                    "source": "alfred",
                    "project": project,
                    "taskId": tid,
                    "fullTitle": chosen.title,
                    "deadlineHuman": "",  # preenchido pelo caller se necessário
                    "part": None,  # preenchido abaixo se multi-dia
                })

                remaining[tid] = max(0, remaining[tid] - duration)
                cursor = block_end + timedelta(minutes=BREAK_MIN)

    # Preencher part labels para tasks com múltiplos blocos
    for tid, indices in task_block_indices.items():
        if len(indices) > 1:
            total = len(indices)
            for part_num, block_idx in enumerate(indices, start=1):
                suggested[block_idx]["part"] = f"{part_num}/{total}"

    return suggested


def _calc_risk(
    tasks: list[Task],
    suggested: list[dict],
    today: date,
    week_end: date,
) -> dict | None:
    """Calcula risco da semana considerando bloqueadas e datas de desbloqueio."""
    needed_min = 0
    risky_tasks: list[dict] = []

    for t in tasks:
        if getattr(t, "blocked", False):
            until = getattr(t, "blocked_until", None)
            if until and until <= week_end:
                # Desbloqueada essa semana — contar
                est = (getattr(t, "estimated_minutes", 120) or 120)
                needed_min += est
                risky_tasks.append({
                    "name": t.title,
                    "hours": est / 60,
                    "deadline_type": getattr(t, "deadline_type", "soft") or "soft",
                    "note": f"desbloqueada a partir de {until}",
                })
            continue

        if not t.deadline:
            continue
        try:
            dl = t.deadline.date() if hasattr(t.deadline, "date") else t.deadline
        except Exception:
            continue
        if dl <= week_end:
            est = t.estimated_minutes or 120
            needed_min += est
            risky_tasks.append({
                "name": t.title,
                "hours": est / 60,
                "deadline_type": getattr(t, "deadline_type", "soft") or "soft",
            })

    if needed_min == 0:
        return None

    # Horas disponíveis = soma de todos os blocos sugeridos (excluindo quick)
    available_min = sum(
        int(
            (
                datetime.strptime(b["end"], "%H:%M") - datetime.strptime(b["start"], "%H:%M")
            ).total_seconds() / 60
        )
        for b in suggested
        if b.get("type") != "quick"
    )

    needed_h = round(needed_min / 60, 1)
    available_h = round(available_min / 60, 1)
    deficit = round(max(0, needed_h - available_h), 1)

    if deficit <= 0:
        return None

    # Sugerir qual task mover (menor, prazo soft)
    suggestion = ""
    soft_tasks = [rt for rt in risky_tasks if rt.get("deadline_type") != "hard"]
    if soft_tasks:
        easiest = min(soft_tasks, key=lambda x: x["hours"])
        name = easiest["name"]
        if "|" in name:
            _, name = name.split("|", 1)
        name = name.strip()
        suggestion = f"Considere mover '{name}' ({easiest['hours']:.0f}h) pra próxima semana."
    else:
        suggestion = f"Déficit de {deficit}h esta semana. Considere adiar tarefas ou trabalhar além do horário padrão."

    return {
        "totalHoursNeeded": needed_h,
        "totalHoursAvailable": available_h,
        "deficit": deficit,
        "suggestion": suggestion,
        "taskCount": len(risky_tasks),
    }


def _find_quick_tasks_for_gaps(
    suggested_blocks: list[dict],
    existing_blocks: list[AgendaBlock],
    tasks: list[Task],
    day_idx: int,
    day_date: date,
) -> list[dict]:
    """Encontra gaps de 20-45min entre blocos e sugere tasks rápidas."""
    quick_suggestions: list[dict] = []

    # Juntar todos os blocos do dia (gcal + sugeridos) e ordenar por hora
    all_blocks: list[dict] = []
    for b in suggested_blocks:
        if b.get("day") == day_idx:
            try:
                s = int(datetime.strptime(b["start"], "%H:%M").hour * 60 + datetime.strptime(b["start"], "%H:%M").minute)
                e = int(datetime.strptime(b["end"], "%H:%M").hour * 60 + datetime.strptime(b["end"], "%H:%M").minute)
                all_blocks.append({"start": s, "end": e})
            except Exception:
                pass
    for ev in existing_blocks:
        if ev.start_at and ev.end_at and ev.start_at.date() == day_date and ev.status != "cancelled":
            sa = ev.start_at.replace(tzinfo=None) if ev.start_at.tzinfo else ev.start_at
            ea = ev.end_at.replace(tzinfo=None) if ev.end_at.tzinfo else ev.end_at
            all_blocks.append({"start": sa.hour * 60 + sa.minute, "end": ea.hour * 60 + ea.minute})

    all_blocks.sort(key=lambda x: x["start"])

    # Encontrar gaps de 20-45min
    for i in range(len(all_blocks) - 1):
        gap_start = all_blocks[i]["end"]
        gap_end = all_blocks[i + 1]["start"]
        gap_minutes = gap_end - gap_start

        if 20 <= gap_minutes <= 45:
            # Buscar task com estimativa <= gap_minutes
            for t in tasks:
                est = getattr(t, "estimated_minutes", 120) or 120
                if est <= gap_minutes and not getattr(t, "blocked", False):
                    # Não sugerir tasks que já têm bloco sugerido neste dia
                    already = any(
                        b.get("day") == day_idx and b.get("taskId") == str(t.id)
                        for b in suggested_blocks
                    )
                    if already:
                        continue
                    gap_start_dt = datetime(day_date.year, day_date.month, day_date.day, gap_start // 60, gap_start % 60)
                    gap_end_dt = gap_start_dt + timedelta(minutes=est)
                    project, task_name = _parse_project_task(t.title)
                    quick_suggestions.append({
                        "day": day_idx,
                        "title": task_name,
                        "start": gap_start_dt.strftime("%H:%M"),
                        "time": gap_start_dt.strftime("%H:%M"),
                        "end": gap_end_dt.strftime("%H:%M"),
                        "type": "quick",
                        "source": "alfred",
                        "project": project,
                        "taskId": str(t.id),
                        "fullTitle": t.title,
                        "shortTitle": (t.title or "")[:30],
                        "deadlineHuman": "",
                    })
                    break  # Só uma sugestão por gap

    return quick_suggestions


async def create_task_blocks(
    db: AsyncSession,
    task: Task,
    week_start: date,
    week_end: date,
) -> list:
    """Cria blocos de agenda para uma task, respeitando eventos existentes.
    - Blocos contínuos (1 bloco por slot, máx 2h)
    - Só dias úteis (seg-sex), dentro do horário 8h-20h
    - Nunca no passado
    """
    import uuid as _uuid
    WORK_START_M = 8 * 60   # 480
    WORK_END_M = 20 * 60    # 1200
    MAX_BLOCK_M = 120
    MIN_BLOCK_M = 15

    today = today_brt()
    estimate = task.estimated_minutes or 120

    week_start_dt = datetime.combine(week_start, datetime.min.time())
    week_end_dt = datetime.combine(week_end, time(23, 59, 59))

    existing_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= week_start_dt)
        .where(AgendaBlock.start_at <= week_end_dt)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    existing_blocks = existing_result.scalars().all()

    # Mapa de ocupação por dia: {date: [(start_min, end_min), ...]}
    occupied: dict[date, list[tuple[int, int]]] = {}
    for block in existing_blocks:
        if not block.start_at or not block.end_at:
            continue
        d = block.start_at.date()
        s_min = block.start_at.hour * 60 + block.start_at.minute
        e_min = block.end_at.hour * 60 + block.end_at.minute
        occupied.setdefault(d, []).append((s_min, e_min))

    def free_slots_on_day(day: date) -> list[tuple[int, int]]:
        if day.weekday() >= 5 or day < today:
            return []
        busy = sorted(occupied.get(day, []), key=lambda x: x[0])
        free = []
        cursor = WORK_START_M
        for bs, be in busy:
            if cursor < bs and bs - cursor >= MIN_BLOCK_M:
                free.append((cursor, bs))
            cursor = max(cursor, be)
        if cursor < WORK_END_M and WORK_END_M - cursor >= MIN_BLOCK_M:
            free.append((cursor, WORK_END_M))
        return free

    remaining = estimate
    created_blocks = []
    current_day = week_start

    while current_day <= week_end and remaining > 0:
        for slot_start, slot_end in free_slots_on_day(current_day):
            if remaining <= 0:
                break
            available = slot_end - slot_start
            block_duration = min(available, MAX_BLOCK_M, remaining)
            if block_duration < MIN_BLOCK_M:
                continue

            start_dt = datetime.combine(current_day, time(slot_start // 60, slot_start % 60))
            end_dt = start_dt + timedelta(minutes=block_duration)

            new_block = AgendaBlock(
                title=task.title or "",
                start_at=start_dt,
                end_at=end_dt,
                block_type="suggested",
                source="alfred",
                status="planned",
                task_id=task.id,
                pinned=False,
            )
            db.add(new_block)
            created_blocks.append(new_block)
            remaining -= block_duration

            # Atualiza ocupação para não sobrepor mais blocos no mesmo dia
            occupied.setdefault(current_day, []).append((slot_start, slot_start + block_duration))

        current_day += timedelta(days=1)

    await db.commit()
    return created_blocks


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

    from datetime import date as date_type
    today_d = date_type.today()

    def is_task_available(t):
        if not getattr(t, 'blocked', False):
            return True
        until = getattr(t, 'blocked_until', None)
        if until and until <= today_d:
            return True  # desbloqueio previsto já passou ou é hoje
        return False

    work_tasks = [t for t in work_tasks if is_task_available(t)]
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

    # Adicionar sugestões rápidas para gaps entre blocos
    for day_idx, day_date in days:
        quick = _find_quick_tasks_for_gaps(suggested, existing_blocks, work_tasks, day_idx, day_date)
        suggested.extend(quick)

    risk = _calc_risk(work_tasks, suggested, today, week_end)

    return suggested, risk


def find_next_block_for_task(suggested: list[dict], task_id: str) -> str:
    """Retorna o horário de início do próximo bloco sugerido para uma task."""
    for block in suggested:
        if block.get("taskId") == task_id:
            return block["start"]
    return ""


async def recalculate_suggestions(
    db: AsyncSession,
    week_start: date,
    week_end: date,
) -> list[dict]:
    """
    Recalcula blocos sugeridos (pinned=False, source=alfred) para a semana,
    respeitando blocos pinned (usuário) e GCal. Deleta suggested existentes e cria novos.
    """
    from sqlalchemy import delete as sa_delete
    import uuid as _uuid

    today = today_brt()
    now_naive = now_brt_naive()

    monday_dt = datetime.combine(week_start, datetime.min.time())
    friday_dt = datetime.combine(week_end, datetime.max.time().replace(microsecond=0))

    # Deletar blocos sugeridos existentes
    await db.execute(
        sa_delete(AgendaBlock).where(
            AgendaBlock.start_at >= monday_dt,
            AgendaBlock.start_at <= friday_dt,
            AgendaBlock.pinned == False,  # noqa: E712
            AgendaBlock.source.in_(["alfred", "system"]),
        )
    )
    await db.flush()

    # Blocos restantes (pinned + gcal)
    blocks_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= monday_dt)
        .where(AgendaBlock.start_at <= friday_dt)
        .where(AgendaBlock.status != "cancelled")
    )
    existing_blocks = list(blocks_result.scalars().all())

    # Tasks de trabalho ativas
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.category != "personal")
        .limit(30)
    )
    raw_tasks = result.scalars().all()
    work_tasks = [t for t in raw_tasks if not (t.category or "").startswith("personal")]

    from datetime import date as _date
    today_d = _date.today()

    def _is_available(t: Task) -> bool:
        if not getattr(t, "blocked", False):
            return True
        until = getattr(t, "blocked_until", None)
        return bool(until and until <= today_d)

    work_tasks = [t for t in work_tasks if _is_available(t)]
    work_tasks.sort(key=lambda t: _task_priority_key(t, today))

    # Tasks já com bloco pinned na semana — não re-alocar
    pinned_task_ids = {str(b.task_id) for b in existing_blocks if b.pinned and b.task_id}
    unallocated = [t for t in work_tasks if str(t.id) not in pinned_task_ids]

    days: list[tuple[int, date]] = [
        (i, week_start + timedelta(days=i)) for i in range(5)
        if (week_start + timedelta(days=i)) <= week_end
    ]

    suggested_dicts = _allocate(unallocated, days, existing_blocks, now_naive)

    for day_idx, day_date in days:
        quick = _find_quick_tasks_for_gaps(suggested_dicts, existing_blocks, unallocated, day_idx, day_date)
        suggested_dicts.extend(quick)

    # Persistir novos blocos no banco
    for b in suggested_dicts:
        if not b.get("start") or not b.get("end"):
            continue
        day_idx = b["day"]
        day_date = week_start + timedelta(days=day_idx)
        try:
            start_dt = datetime.combine(day_date, datetime.strptime(b["start"], "%H:%M").time())
            end_dt = datetime.combine(day_date, datetime.strptime(b["end"], "%H:%M").time())
        except Exception:
            continue

        task_id_val = None
        if b.get("taskId"):
            try:
                task_id_val = _uuid.UUID(b["taskId"])
            except Exception:
                pass

        block_type = "quick" if b.get("type") == "quick" else "suggested"
        db.add(AgendaBlock(
            title=b.get("fullTitle") or b.get("title") or "",
            start_at=start_dt,
            end_at=end_dt,
            block_type=block_type,
            source="alfred",
            status="planned",
            task_id=task_id_val,
            pinned=False,
            part=b.get("part"),
        ))

    await db.commit()
    return suggested_dicts
