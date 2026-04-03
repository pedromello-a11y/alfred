import re
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, DumpItem, Task
from app.services import agenda_manager, brain, dump_manager, interpreter, task_manager
from app.services.time_utils import now_brt, now_brt_naive, to_brt_naive, today_brt

_PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}
_AGENDA_QUESTION_HINTS = (
    "agenda",
    "horario",
    "horário",
    "reuniao",
    "reunião",
    "o que tenho",
    "o que falta",
    "próximo bloco",
    "proximo bloco",
    "meu dia",
    "como está meu dia",
    "como esta meu dia",
    "como ta meu dia",
)
_FOCUS_QUESTION_HINTS = (
    "foco agora",
    "qual meu foco",
    "em que eu foco",
    "em que devo focar",
    "o que eu faço agora",
    "qual a próxima tarefa",
    "qual a proxima tarefa",
)
_PRIORITY_QUESTION_HINTS = (
    "prioridade do dia",
    "qual a prioridade",
    "principal prioridade",
    "mais importante hoje",
)
_DELAY_QUESTION_HINTS = (
    "atrasado",
    "atrasada",
    "vencido",
    "vencida",
    "passou do prazo",
    "em atraso",
)
_REFERENCE_STOPWORDS = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "para",
    "pra",
    "com",
    "sem",
    "uma",
    "um",
    "na",
    "no",
    "em",
    "por",
    "que",
    "e",
    "o",
    "a",
    "as",
    "os",
}
_REFERENCE_GENERIC = {
    "task",
    "tarefa",
    "agenda",
    "bloco",
    "dump",
    "nota",
    "reuniao",
    "reunioes",
    "projeto",
}
_STATUS_LABELS = {
    "pending": "pendente",
    "in_progress": "em andamento",
    "done": "concluída",
}


async def _remember_last_action(kind: str, object_id: str, db: AsyncSession) -> None:
    await task_manager.set_setting("last_action_type", kind, db)
    await task_manager.set_setting("last_action_id", object_id, db)
    if kind == "task":
        await task_manager.set_setting("last_created_task_id", object_id, db)


async def _append_setting_log(key: str, value: str, db: AsyncSession, limit: int = 4000) -> None:
    existing = await task_manager.get_setting(key, "", db=db) or ""
    timestamp = now_brt().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {value.strip()}"
    merged = f"{existing}\n{entry}".strip() if existing else entry
    if len(merged) > limit:
        merged = merged[-limit:]
    await task_manager.set_setting(key, merged, db)



# ── Parser de datas naturais PT-BR + fluxo de prazo ──────────────────
_NL_DATE_PATTERNS = [
    (_re_mod.compile(r"(?i)(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?"), "dmy"),
    (_re_mod.compile(r"(?i)dia\s+(\d{1,2})"), "day_only"),
    (_re_mod.compile(r"(?i)amanh[aã]"), "tomorrow"),
    (_re_mod.compile(r"(?i)hoje"), "today"),
    (_re_mod.compile(r"(?i)segunda"), "wd_0"),
    (_re_mod.compile(r"(?i)ter[cç]a"), "wd_1"),
    (_re_mod.compile(r"(?i)quarta"), "wd_2"),
    (_re_mod.compile(r"(?i)quinta"), "wd_3"),
    (_re_mod.compile(r"(?i)sexta"), "wd_4"),
    (_re_mod.compile(r"(?i)s[aá]bado"), "wd_5"),
    (_re_mod.compile(r"(?i)domingo"), "wd_6"),
]
_NL_TIME_RE = _re_mod.compile(r"(?i)(?:às?|as|ate|até)\s*(\d{1,2})(?::(\d{2}))?\s*h?")
_NL_EOD_RE = _re_mod.compile(r"(?i)(fim do dia|final do dia|eod)")
_DIAS_SEMANA_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _parse_natural_date(text: str) -> datetime | None:
    """Parseia datas naturais em português. Retorna datetime naive BRT."""
    from app.services.time_utils import today_brt
    today = today_brt()
    target_date = None

    for pattern, kind in _NL_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if kind == "today":
            target_date = today
        elif kind == "tomorrow":
            target_date = today + timedelta(days=1)
        elif kind.startswith("wd_"):
            target_wd = int(kind.split("_")[1])
            days_ahead = target_wd - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target_date = today + timedelta(days=days_ahead)
        elif kind == "day_only":
            day_num = int(match.group(1))
            try:
                from datetime import date as _date_type
                target_date = today.replace(day=day_num)
                if target_date < today:
                    m = today.month + 1
                    y = today.year
                    if m > 12:
                        m, y = 1, y + 1
                    target_date = _date_type(y, m, day_num)
            except ValueError:
                continue
        elif kind == "dmy":
            day_num = int(match.group(1))
            month_num = int(match.group(2))
            yr = match.group(3)
            year_num = int(yr) if yr else today.year
            if year_num < 100:
                year_num += 2000
            try:
                from datetime import date as _date_type
                target_date = _date_type(year_num, month_num, day_num)
            except ValueError:
                continue
        if target_date:
            break

    if not target_date:
        return None

    hour, minute = 23, 59
    tm = _NL_TIME_RE.search(text)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
    elif _NL_EOD_RE.search(text):
        hour, minute = 23, 59

    return datetime(target_date.year, target_date.month, target_date.day, hour, minute)


async def _handle_deadline_response(raw_text: str, task_id_str: str, db: AsyncSession) -> tuple[str, bool]:
    """Tenta interpretar a mensagem como um prazo para a task pendente."""
    from uuid import UUID as _UUID

    parsed = _parse_natural_date(raw_text)
    if not parsed:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    try:
        task_uuid = _UUID(task_id_str)
    except ValueError:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    task.deadline = parsed
    await db.commit()
    await db.refresh(task)
    await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)

    dia = _DIAS_SEMANA_PT[parsed.weekday()]
    data_fmt = parsed.strftime("%d/%m/%Y")
    hora_fmt = parsed.strftime("%H:%M") if not (parsed.hour == 23 and parsed.minute == 59) else "fim do dia"

    lines = [
        f"\u2705 Prazo definido: *{data_fmt}* ({dia})",
        "",
        f"*{task.title}*",
        f"Prazo: {data_fmt} — {hora_fmt}",
    ]
    hint = await _current_or_next_focus_hint(db)
    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines), True



def _compose_title(task_title: str, project: str | None = None) -> str:
    title = (task_title or "").strip()
    project_name = (project or "").strip()
    if project_name and title:
        return f"{project_name} | {title}"
    return title or project_name


def _to_naive_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return to_brt_naive(datetime.fromisoformat(value))


def _reference_tokens(value: str) -> set[str]:
    normalized = task_manager.normalize_task_title(value or "")
    tokens: set[str] = set()
    for token in normalized.split():
        if token in _REFERENCE_STOPWORDS:
            continue
        if len(token) >= 3 or any(ch.isdigit() for ch in token):
            tokens.add(token)
    return tokens


def _reference_match_score(reference: str, *candidate_texts: str) -> int:
    ref_norm = task_manager.normalize_task_title(reference or "")
    if not ref_norm:
        return 0
    ref_tokens = _reference_tokens(reference)
    best = 0
    for text in candidate_texts:
        cand_norm = task_manager.normalize_task_title(text or "")
        if not cand_norm:
            continue
        score = 0
        if ref_norm == cand_norm:
            score += 180
        elif ref_norm in cand_norm:
            score += 80
        elif cand_norm in ref_norm and len(cand_norm) >= 8:
            score += 30
        cand_tokens = _reference_tokens(text or "")
        overlap = ref_tokens & cand_tokens
        if overlap:
            score += len(overlap) * 14
            rare = [token for token in overlap if token not in _REFERENCE_GENERIC]
            score += len(rare) * 5
            if ref_tokens and overlap == ref_tokens:
                score += 18
        else:
            score -= 4
        if len(ref_tokens) == 1 and len(overlap) == 1:
            token = next(iter(ref_tokens))
            if token in _REFERENCE_GENERIC:
                score -= 12
            else:
                score += 8
        best = max(best, score)
    return best


def _reference_match_is_viable(reference: str, score: int, *candidate_texts: str) -> bool:
    ref_tokens = _reference_tokens(reference)
    overlap: set[str] = set()
    for text in candidate_texts:
        overlap |= ref_tokens & _reference_tokens(text or "")
    if score >= 70:
        return True
    if len(overlap) >= 2 and score >= 20:
        return True
    if ref_tokens and overlap == ref_tokens and score >= 16:
        return True
    if len(ref_tokens) == 1 and len(overlap) == 1 and score >= 24:
        token = next(iter(ref_tokens))
        return token not in _REFERENCE_GENERIC
    return False


def _format_deadline_brief(deadline: datetime | None) -> str | None:
    if not deadline:
        return None
    today = today_brt()
    target = deadline.date()
    time_label = deadline.strftime('%H:%M')
    has_meaningful_time = time_label != '00:00'
    if target == today:
        return f"hoje às {time_label}" if has_meaningful_time else "hoje"
    if target == today + timedelta(days=1):
        return f"amanhã às {time_label}" if has_meaningful_time else "amanhã"
    return deadline.strftime('%d/%m às %H:%M') if has_meaningful_time else deadline.strftime('%d/%m')


def _task_display_name(task: Task | None) -> str:
    if not task:
        return ""
    return task.title or ""


async def _current_or_next_focus_hint(db: AsyncSession, exclude_task_id: UUID | None = None) -> str | None:
    now_naive = now_brt_naive()
    today = today_brt()
    current_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at <= now_naive)
        .where(AgendaBlock.end_at > now_naive)
        .order_by(AgendaBlock.start_at.desc())
        .limit(1)
    )
    current_block = current_result.scalar_one_or_none()
    if current_block:
        if current_block.block_type == "break":
            return f"Você está em descanso até {current_block.end_at.strftime('%H:%M')}."
        return f"Foco agora: *{current_block.title}* até {current_block.end_at.strftime('%H:%M')}."

    upcoming_result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at > now_naive)
        .where(AgendaBlock.start_at < datetime.combine(today, time.max))
        .order_by(AgendaBlock.start_at.asc())
        .limit(1)
    )
    upcoming_block = upcoming_result.scalar_one_or_none()

    active_tasks = list(await task_manager.get_active_tasks(db))
    if exclude_task_id:
        active_tasks = [task for task in active_tasks if task.id != exclude_task_id]
    due_today = [task for task in active_tasks if task.deadline and task.deadline.date() == today]
    due_today.sort(key=lambda task: (task.deadline, task.priority or 99))

    if due_today:
        first = due_today[0]
        deadline = _format_deadline_brief(first.deadline)
        if deadline:
            return f"Próximo foco sugerido: *{_task_display_name(first)}* ({deadline})."
        return f"Próximo foco sugerido: *{_task_display_name(first)}*."
    if active_tasks:
        return f"Próximo foco sugerido: *{_task_display_name(active_tasks[0])}*."
    if upcoming_block:
        return f"Próximo bloco: *{upcoming_block.title}* às {upcoming_block.start_at.strftime('%H:%M')}."
    return None


async def _build_new_task_response(task: Task, db: AsyncSession) -> str:
    lines = [f"Anotado: *{task.title}*."]
    deadline = _format_deadline_brief(task.deadline)
    if deadline:
        lines.append(f"Prazo: {deadline}.")
        hint = await _current_or_next_focus_hint(db)
        if hint:
            lines.append(hint)
    else:
        await task_manager.set_setting("awaiting_deadline_for_task_id", str(task.id), db)
        lines.append("")
        lines.append("Qual o prazo de entrega? (ex: \\"dia 07\\", \\"sexta\\", \\"07/04\\")")
    return "\n".join(lines)


async def _build_dump_response(dump: DumpItem, db: AsyncSession) -> str:
    lines = [f"Guardei em dumps como *{dump.rewritten_title}*."]
    if dump.category:
        lines.append(f"Categoria: {dump.category}.")
    hint = await _current_or_next_focus_hint(db)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


async def _build_task_update_response(task: Task, db: AsyncSession) -> str:
    status_label = _STATUS_LABELS.get(task.status, task.status)
    lines = [f"Atualizado: *{task.title}* → {status_label}."]
    if task.status in {"pending", "in_progress"}:
        deadline = _format_deadline_brief(task.deadline)
        if deadline:
            lines.append(f"Prazo: {deadline}.")
    hint = await _current_or_next_focus_hint(db, exclude_task_id=task.id if task.status == "done" else None)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


async def _append_note_to_task(task: Task, note_text: str, db: AsyncSession) -> Task:
    timestamp = now_brt().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {note_text.strip()}"
    task.notes = f"{task.notes or ''}\n{entry}".strip()
    await db.commit()
    await db.refresh(task)
    return task


async def _store_system_feedback(decision: dict[str, Any], db: AsyncSession):
    text = decision.get("note") or decision.get("raw_text") or ""
    await task_manager.set_setting("last_system_feedback", text, db)
    await _append_setting_log("system_feedback_log", text, db)
    await _remember_last_action("system_feedback", "settings", db)
    return "Entendi. Guardei isso como ajuste de comportamento do sistema. Não virou task nem bloco de agenda."


async def _store_context_note(decision: dict[str, Any], db: AsyncSession):
    note_text = decision.get("note") or decision.get("raw_text") or ""
    reference = decision.get("reference_title") or decision.get("task_title")
    task = None
    if reference:
        task = await task_manager.find_task_by_title_like(reference, db, include_closed=True, include_system=True)
    if task:
        await _append_note_to_task(task, note_text, db)
        await _remember_last_action("task", str(task.id), db)
        return f"Entendi. Guardei isso como nota em *{task.title}*."
    await task_manager.set_setting("last_context_note", note_text, db)
    await _append_setting_log("context_note_log", note_text, db)
    await _remember_last_action("note", "context", db)
    return "Entendi. Guardei isso como nota contextual, sem virar demanda operacional."


async def _create_task_from_interpretation(decision: dict[str, Any], origin: str, db: AsyncSession):
    title = _compose_title(decision.get("task_title") or decision.get("reference_title") or decision.get("raw_text", "")[:80], decision.get("project"))
    deadline = _to_naive_datetime(decision.get("deadline_iso"))
    priority = _PRIORITY_MAP.get((decision.get("note") or "").lower(), None)
    task = Task(
        title=task_manager.canonicalize_task_title(title),
        origin=origin,
        status="pending",
        priority=priority,
        deadline=deadline,
        category=decision.get("category") or "work",
        effort_type="project",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await _remember_last_action("task", str(task.id), db)
    return task


async def _persist_blocks_from_interpretation(decision: dict[str, Any], origin: str, db: AsyncSession):
    persisted: list[AgendaBlock] = []
    for block in decision.get("time_blocks") or []:
        start_at = _to_naive_datetime(block.get("start_at"))
        end_at = _to_naive_datetime(block.get("end_at"))
        if not start_at or not end_at or end_at <= start_at:
            continue
        persisted.append(
            await agenda_manager.upsert_agenda_block(
                title=block.get("title") or "Bloco",
                start_at=start_at,
                end_at=end_at,
                block_type=block.get("block_type") or "focus",
                source=origin,
                db=db,
                notes=decision.get("raw_text", "")[:300],
            )
        )
    if persisted:
        await _remember_last_action("agenda_block", str(persisted[-1].id), db)
    return persisted


async def _update_task_from_interpretation(decision: dict[str, Any], db: AsyncSession):
    reference = decision.get("reference_title") or decision.get("task_title")
    if not reference:
        return None
    task = await task_manager.find_task_by_title_like(reference, db, include_closed=True)
    if not task:
        return None
    status = decision.get("task_status") or "in_progress"
    if status not in {"pending", "in_progress", "done"}:
        status = "in_progress"
    task = await task_manager.update_task_status(task, status, db, note=decision.get("note") or None)
    await _remember_last_action("task", str(task.id), db)
    return task


async def _find_task_correction_target(reference: str | None, last_type: str | None, last_id: str | None, db: AsyncSession) -> Task | None:
    if reference:
        task = await task_manager.find_task_by_title_like(reference, db, include_closed=True, include_system=True)
        if task:
            return task
    if last_type == "task" and last_id:
        try:
            task_uuid = UUID(last_id)
        except ValueError:
            return None
        result = await db.execute(select(Task).where(Task.id == task_uuid).limit(1))
        return result.scalar_one_or_none()
    return None


async def _find_dump_correction_target(reference: str | None, last_type: str | None, last_id: str | None, db: AsyncSession) -> DumpItem | None:
    if reference:
        result = await db.execute(select(DumpItem).order_by(DumpItem.created_at.desc()).limit(80))
        dumps = list(result.scalars().all())
        best_dump: DumpItem | None = None
        best_score = 0
        for dump in dumps:
            score = _reference_match_score(reference, dump.rewritten_title or "", dump.raw_text or "")
            if not _reference_match_is_viable(reference, score, dump.rewritten_title or "", dump.raw_text or ""):
                continue
            if score > best_score:
                best_dump = dump
                best_score = score
        if best_dump:
            return best_dump
    if last_type == "dump" and last_id:
        try:
            dump_uuid = UUID(last_id)
        except ValueError:
            return None
        result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid).limit(1))
        return result.scalar_one_or_none()
    return None


async def _find_agenda_correction_target(reference: str | None, last_type: str | None, last_id: str | None, db: AsyncSession) -> AgendaBlock | None:
    if reference:
        result = await db.execute(select(AgendaBlock).order_by(AgendaBlock.start_at.desc()).limit(80))
        blocks = list(result.scalars().all())
        best_block: AgendaBlock | None = None
        best_score = 0
        for block in blocks:
            score = _reference_match_score(reference, block.title or "", block.notes or "")
            if not _reference_match_is_viable(reference, score, block.title or "", block.notes or ""):
                continue
            if score > best_score:
                best_block = block
                best_score = score
        if best_block:
            return best_block
    if last_type == "agenda_block" and last_id:
        try:
            block_uuid = UUID(last_id)
        except ValueError:
            return None
        result = await db.execute(select(AgendaBlock).where(AgendaBlock.id == block_uuid).limit(1))
        return result.scalar_one_or_none()
    return None


async def _apply_correction(decision: dict[str, Any], origin: str, db: AsyncSession):
    correction_type = (decision.get("correction_new_type") or "").strip()
    last_type = await task_manager.get_setting("last_action_type", db=db)
    last_id = await task_manager.get_setting("last_action_id", db=db)
    reference = decision.get("reference_title") or decision.get("task_title")

    if correction_type == "dump":
        task = await _find_task_correction_target(reference, last_type, last_id, db)
        if not task:
            return None, "Não encontrei a task para mover para dumps."
        payload = decision.get("reference_title") or task.title
        dump = await dump_manager.create_dump_item(payload, origin, db, source_task_id=task.id)
        await db.delete(task)
        await db.commit()
        await _remember_last_action("dump", str(dump.id), db)
        return dump, f"Corrigido. Movi *{task.title}* para dumps como *{dump.rewritten_title}*."

    if correction_type == "task":
        dump = await _find_dump_correction_target(reference, last_type, last_id, db)
        if dump:
            task_title = decision.get("task_title") or decision.get("reference_title") or dump.rewritten_title or dump.raw_text
            combined = _compose_title(task_title, decision.get("project"))
            task = Task(
                title=task_manager.canonicalize_task_title(combined),
                origin=origin,
                status=decision.get("task_status") or "pending",
                deadline=_to_naive_datetime(decision.get("deadline_iso")),
                category=decision.get("category") or "work",
                effort_type="project",
                notes=f"Criada via correção do dump: {dump.raw_text}",
            )
            db.add(task)
            await db.flush()
            await db.delete(dump)
            await db.commit()
            await db.refresh(task)
            await _remember_last_action("task", str(task.id), db)
            return task, f"Corrigido. Transformei o dump em task: *{task.title}*."

        block = await _find_agenda_correction_target(reference, last_type, last_id, db)
        if block:
            task_title = decision.get("task_title") or decision.get("reference_title") or block.title
            combined = _compose_title(task_title, decision.get("project"))
            task = Task(
                title=task_manager.canonicalize_task_title(combined),
                origin=origin,
                status=decision.get("task_status") or "pending",
                deadline=_to_naive_datetime(decision.get("deadline_iso")),
                category=decision.get("category") or "work",
                effort_type="project",
                notes=f"Criada via correção de bloco de agenda: {block.title} {block.start_at.isoformat()}->{block.end_at.isoformat()}",
            )
            db.add(task)
            await db.flush()
            await db.delete(block)
            await db.commit()
            await db.refresh(task)
            await _remember_last_action("task", str(task.id), db)
            return task, f"Corrigido. Transformei o bloco de agenda em task: *{task.title}*."

        return None, "Entendi como correção para task, mas não achei dump ou bloco compatível."

    if correction_type == "agenda_block":
        task = await _find_task_correction_target(reference, last_type, last_id, db)
        if not task:
            return None, "Não encontrei a task para mover para agenda."
        blocks = await _persist_blocks_from_interpretation(decision, origin, db)
        if not blocks:
            return None, "Entendi como correção para agenda, mas faltou bloco de horário válido."
        await db.delete(task)
        await db.commit()
        await _remember_last_action("agenda_block", str(blocks[-1].id), db)
        return blocks[-1], "Corrigido. Removi a task e registrei isso na agenda.\n" + _agenda_response(blocks)

    if correction_type == "note":
        task = await _find_task_correction_target(reference, last_type, last_id, db)
        if not task:
            return None, "Não encontrei a task para converter em nota."
        note_text = decision.get("note") or f"Convertida para nota contextual: {task.title}"
        await db.delete(task)
        active = list(await task_manager.get_active_tasks(db))
        if active:
            target = await _append_note_to_task(active[0], note_text, db)
            await _remember_last_action("task", str(target.id), db)
            return target, f"Corrigido. Removi a task e guardei isso como nota em *{target.title}*."
        await db.commit()
        await task_manager.set_setting("last_context_note", note_text, db)
        await _append_setting_log("context_note_log", note_text, db)
        await _remember_last_action("note", "context", db)
        return None, "Corrigido. Removi a task e guardei isso como nota contextual."

    return None, "Entendi como correção, mas essa conversão ainda não está suportada automaticamente."


def _agenda_response(blocks: list[AgendaBlock]) -> str:
    lines = ["Agenda registrada:"]
    for block in blocks[:5]:
        lines.append(f"- {block.title} — {block.start_at.strftime('%d/%m %H:%M')}→{block.end_at.strftime('%H:%M')}")
    return "\n".join(lines)


def _looks_like_agenda_question(raw_text: str) -> bool:
    lowered = (raw_text or "").lower()
    return any(token in lowered for token in _AGENDA_QUESTION_HINTS)


def _looks_like_focus_question(raw_text: str) -> bool:
    lowered = (raw_text or "").lower()
    return any(token in lowered for token in _FOCUS_QUESTION_HINTS)


def _looks_like_priority_question(raw_text: str) -> bool:
    lowered = (raw_text or "").lower()
    return any(token in lowered for token in _PRIORITY_QUESTION_HINTS)


def _looks_like_delay_question(raw_text: str) -> bool:
    lowered = (raw_text or "").lower()
    return any(token in lowered for token in _DELAY_QUESTION_HINTS)


async def _build_question_context(db: AsyncSession) -> str:
    now = now_brt()
    tasks = list(await task_manager.get_active_tasks(db))[:10]
    today = today_brt()
    start_day = datetime.combine(today, time.min)
    end_day = start_day + timedelta(days=1)
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= start_day)
        .where(AgendaBlock.start_at < end_day)
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = list(result.scalars().all())

    task_lines = [
        f"- {task.title} | status={task.status} | prazo={(task.deadline.strftime('%d/%m %H:%M') if task.deadline else 'sem prazo')}"
        for task in tasks
    ] or ["- nenhuma task ativa"]
    block_lines = [
        f"- {block.title} | {block.start_at.strftime('%H:%M')}->{block.end_at.strftime('%H:%M')} | tipo={block.block_type}"
        for block in blocks
    ] or ["- nenhum bloco hoje"]
    return (
        f"Hora atual BRT: {now.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Agenda de hoje:\n" + "\n".join(block_lines) + "\n\n"
        f"Tasks ativas:\n" + "\n".join(task_lines)
    )


async def _handle_operational_agenda_question(db: AsyncSession) -> str:
    now = now_brt()
    now_naive = now_brt_naive()
    today = today_brt()
    start_day = datetime.combine(today, time.min)
    end_day = start_day + timedelta(days=1)
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= start_day)
        .where(AgendaBlock.start_at < end_day)
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = list(result.scalars().all())
    active_tasks = list(await task_manager.get_active_tasks(db))
    due_today = [t for t in active_tasks if t.deadline and t.deadline.date() == today][:5]

    lines = [f"Agora: {now.strftime('%H:%M')} BRT."]
    current = None
    upcoming = None
    if blocks:
        lines.append("Agenda de hoje:")
        for block in blocks:
            if block.end_at <= now_naive:
                marker = "✅"
            elif block.start_at <= now_naive < block.end_at:
                marker = "▶️"
                current = block
            else:
                marker = "⏰"
                if upcoming is None:
                    upcoming = block
            lines.append(f"{marker} {block.start_at.strftime('%H:%M')}→{block.end_at.strftime('%H:%M')} — {block.title}")
    else:
        lines.append("Agenda de hoje: sem blocos registrados.")

    if due_today:
        lines.append("\nTarefas com prazo hoje:")
        for task in due_today:
            deadline_label = task.deadline.strftime('%H:%M') if task.deadline else 'hoje'
            lines.append(f"- {task.title} ({deadline_label})")

    if current:
        if current.block_type == "break":
            lines.append(f"\nVocê está em descanso até {current.end_at.strftime('%H:%M')}.")
        else:
            lines.append(f"\nVocê está em: *{current.title}* até {current.end_at.strftime('%H:%M')}.")
    elif upcoming:
        lines.append(f"\nPróximo bloco: *{upcoming.title}* às {upcoming.start_at.strftime('%H:%M')}.")
    elif due_today:
        lines.append(f"\nPróximo foco sugerido: *{due_today[0].title}*.")
    else:
        lines.append("\nSem próximo bloco registrado agora.")

    return "\n".join(lines)


async def _handle_focus_question(db: AsyncSession) -> str:
    now_naive = now_brt_naive()
    today = today_brt()
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at <= now_naive)
        .where(AgendaBlock.end_at > now_naive)
        .order_by(AgendaBlock.start_at.desc())
        .limit(1)
    )
    current_block = result.scalar_one_or_none()
    active_tasks = list(await task_manager.get_active_tasks(db))
    due_today = [t for t in active_tasks if t.deadline and t.deadline.date() == today]
    due_today.sort(key=lambda t: t.deadline)

    if current_block:
        if current_block.block_type == "break":
            return f"Agora você está em descanso até {current_block.end_at.strftime('%H:%M')}. Quando voltar, o foco sugerido é *{(due_today[0].title if due_today else (active_tasks[0].title if active_tasks else 'nenhuma task ativa'))}*."
        return f"Seu foco agora é *{current_block.title}* até {current_block.end_at.strftime('%H:%M')}."
    if due_today:
        return f"Seu foco sugerido agora é *{due_today[0].title}* — é a task com prazo mais próximo hoje."
    if active_tasks:
        return f"Seu foco sugerido agora é *{active_tasks[0].title}*."
    return "Agora você não tem task ativa registrada."


async def _handle_priority_question(db: AsyncSession) -> str:
    today = today_brt()
    active_tasks = list(await task_manager.get_active_tasks(db))
    due_today = [t for t in active_tasks if t.deadline and t.deadline.date() == today]
    due_today.sort(key=lambda t: (t.deadline, t.priority or 99))
    if due_today:
        first = due_today[0]
        return f"A prioridade do dia é *{first.title}* — prazo hoje às {first.deadline.strftime('%H:%M')}."
    if active_tasks:
        return f"A prioridade mais clara agora é *{active_tasks[0].title}*."
    return "Não encontrei prioridade ativa registrada agora."


async def _handle_delay_question(db: AsyncSession) -> str:
    today = today_brt()
    active_tasks = list(await task_manager.get_active_tasks(db))
    delayed = [t for t in active_tasks if t.deadline and t.deadline.date() < today]
    delayed.sort(key=lambda t: t.deadline)
    if not delayed:
        return "Não encontrei tarefas atrasadas ativas agora."
    lines = ["Tarefas atrasadas:"]
    for task in delayed[:5]:
        lines.append(f"- {task.title} (prazo {task.deadline.strftime('%d/%m %H:%M')})")
    return "\n".join(lines)


async def _handle_unknown_without_legacy(raw_text: str, db: AsyncSession):
    if agenda_manager.looks_like_agenda_input(raw_text):
        blocks = await agenda_manager.capture_agenda_from_text(raw_text, db, source="whatsapp")
        if blocks:
            await _remember_last_action("agenda_block", str(blocks[-1].id), db)
            return _agenda_response(blocks), "agenda_add_fallback"
    text = (raw_text or "").strip().lower()
    if text.startswith("dump:"):
        dump = await dump_manager.create_dump_item(raw_text, "whatsapp", db)
        await _remember_last_action("dump", str(dump.id), db)
        return f"Registrado em dumps como *{dump.rewritten_title}* ({dump.category or 'desconhecido'}).", "dump_fallback"
    if _looks_like_agenda_question(raw_text):
        return await _handle_operational_agenda_question(db), "question"
    if _looks_like_focus_question(raw_text):
        return await _handle_focus_question(db), "question"
    if _looks_like_priority_question(raw_text):
        return await _handle_priority_question(db), "question"
    if _looks_like_delay_question(raw_text):
        return await _handle_delay_question(db), "question"
    return None, None


async def _legacy_fallback(raw_text: str, origin: str, db):
    """Fallback to legacy message_handler. Lazy import avoids circular dependency."""
    from app.services import message_handler
    return await message_handler.handle(raw_text, origin=origin, db=db)


def _make_item(origin: str, raw_text: str, item_type: str, extracted_title: str):
    """Creates an InboundItem lazily to avoid circular import at module level."""
    from app.services.message_handler import InboundItem
    return InboundItem(item_type=item_type, origin=origin, raw_text=raw_text, extracted_title=extracted_title)


async def handle(raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None):
    if db is None:
        return await _legacy_fallback(raw_text, origin, db)

    # ── Checar se estamos aguardando prazo de task recém-criada ───────
    _awaiting_dl = await task_manager.get_setting("awaiting_deadline_for_task_id", db=db)
    if _awaiting_dl:
        _dl_response, _dl_handled = await _handle_deadline_response(raw_text, _awaiting_dl, db)
        if _dl_handled:
            return _make_item(origin, raw_text, "update", "deadline_set"), _dl_response, "deadline_set"
    # ── Fim check prazo ──────────────────────────────────────────────

    decision = await interpreter.interpret_message(raw_text, db)
    if not decision or decision.get("confidence", 0) < 0.6:
        response, classification = await _handle_unknown_without_legacy(raw_text, db)
        if response:
            return _make_item(origin, raw_text, "idea", classification or "fallback"), response, classification or "fallback"
        return await _legacy_fallback(raw_text, origin, db)

    intent = decision.get("intent")
    if intent == "new_task":
        task = await _create_task_from_interpretation(decision, origin, db)
        return _make_item(origin, raw_text, "task", task.title), await _build_new_task_response(task, db), "new_task"

    if intent == "dump":
        dump = await dump_manager.create_dump_item(raw_text, origin, db)
        await _remember_last_action("dump", str(dump.id), db)
        return _make_item(origin, raw_text, "idea", dump.rewritten_title), await _build_dump_response(dump, db), "dump"

    if intent == "agenda_add":
        blocks = await _persist_blocks_from_interpretation(decision, origin, db)
        if blocks:
            return _make_item(origin, raw_text, "update", blocks[-1].title), _agenda_response(blocks), "agenda_add"
        return await _legacy_fallback(raw_text, origin, db)

    if intent == "task_update":
        task = await _update_task_from_interpretation(decision, db)
        if task:
            return _make_item(origin, raw_text, "update", task.title), await _build_task_update_response(task, db), "task_update"
        return await _legacy_fallback(raw_text, origin, db)

    if intent == "correction":
        obj, response = await _apply_correction(decision, origin, db)
        return _make_item(origin, raw_text, "update", decision.get("correction_new_type") or "correction"), response, "correction"

    if intent == "system_feedback":
        response = await _store_system_feedback(decision, db)
        return _make_item(origin, raw_text, "idea", "system_feedback"), response, "system_feedback"

    if intent == "context_note":
        response = await _store_context_note(decision, db)
        return _make_item(origin, raw_text, "idea", "context_note"), response, "context_note"

    if intent == "question":
        if _looks_like_agenda_question(raw_text):
            response = await _handle_operational_agenda_question(db)
        elif _looks_like_focus_question(raw_text):
            response = await _handle_focus_question(db)
        elif _looks_like_priority_question(raw_text):
            response = await _handle_priority_question(db)
        elif _looks_like_delay_question(raw_text):
            response = await _handle_delay_question(db)
        else:
            context = await _build_question_context(db)
            response = await brain.answer_question(raw_text, context, db=db)
        return _make_item(origin, raw_text, "idea", "question"), response, "question"

    if intent == "chat":
        response = await brain.casual_response(raw_text, db=db)
        return _make_item(origin, raw_text, "idea", "chat"), response, "chat"

    if intent == "unknown":
        response, classification = await _handle_unknown_without_legacy(raw_text, db)
        if response:
            return _make_item(origin, raw_text, "idea", classification or "unknown"), response, classification or "unknown"
        return await _legacy_fallback(raw_text, origin, db)

    return await _legacy_fallback(raw_text, origin, db)
