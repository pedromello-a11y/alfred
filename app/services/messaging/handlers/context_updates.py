"""
Handlers para atualizações de contexto e status de tarefas:
- handle_context_update
- handle_explicit_done_update
- extract_status_updates
- looks_like_context_update / _operational / _explicit_done
"""
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import agenda_manager, task_manager
from app.services.messaging.handlers.parsing import (
    detect_status,
    extract_estimated_minutes,
    extract_note,
    extract_title_candidate,
    field_line_note,
    infer_category,
    is_note_only_candidate,
    looks_like_field_line,
    looks_like_section_header,
    match_task_for_chunk,
    should_skip_chunk,
    split_update_chunks,
)
from app.services.messaging.handlers.utils import (
    agenda_blocks_inline,
    agenda_capture_response,
    capture_context_note,
    format_agenda_block,
    map_status_to_task_status,
    status_label,
)

# ── Padrões (mantidos aqui pois _looks_like_* são usados pelo message_handler) ─

_CONTEXT_UPDATE_HINTS = re.compile(
    r"(?i)(resumo de demandas|demandas ativas|itens já resolvidos|itens ja resolvidos|detalhe[, ]|"
    r"contexto de trabalho|status:|estimativa:|prioridade:|já foi feito|ja foi feito|já terminei|"
    r"ja terminei|já entregou|ja entregou|já startei|ja startei|já comecei|ja comecei|combinei de)"
)
_EXPLICIT_DONE = re.compile(
    r"(?i)\b(terminei|finalizei|concluí|conclui|entreguei|foi entregue|foi aprovado|resolvido|"
    r"resolvida|concluído|concluída|concluida)\b"
)
_NEGATED_DONE = re.compile(
    r"(?i)(ainda não terminei|ainda nao terminei|não terminei|nao terminei|não conclu[ií]|"
    r"nao conclu[ií]|não entreguei|nao entreguei|ainda falta)"
)
_NEGATED_NOT_STARTED = re.compile(r"(?i)(ainda não comecei|ainda nao comecei|não comecei|nao comecei)")
_ACTIVE_TASKS_QUERY = re.compile(
    r"(?i)^(quais (são|sao) )?(minhas )?(tarefas|demandas|atividades) (ativas|em aberto|abertas)\??$|"
    r"^(me diga )?(minhas )?(demandas|atividades) (abertas|ativas)\??$|"
    r"^todas atividades que tem em aberto\??$|"
    r"^(o )?que (tenho|está|esta) (em aberto|aberto agora|ativo agora)\??$"
)
_NOTE_ONLY_HINTS = re.compile(r"(?i)(briefing|keyframes?|reuni[aã]o|3k|alinhar|alinhamento|assets prontos|assets chegaram|storyboard)")


# ── Predicados públicos (usados pelo message_handler como _looks_like_*) ──────

def looks_like_context_update(raw_text: str) -> bool:
    return len(raw_text) >= 240 or bool(_CONTEXT_UPDATE_HINTS.search(raw_text))


def looks_like_operational_status_update(raw_text: str) -> bool:
    if len(raw_text) > 220:
        return False
    if _ACTIVE_TASKS_QUERY.match(raw_text):
        return False
    status = detect_status(raw_text)
    if not status:
        return False
    title = extract_title_candidate(raw_text)
    if not title:
        return False
    if is_note_only_candidate(title):
        return False
    normalized = task_manager.normalize_task_title(title)
    if normalized.startswith("esse e um resumo"):
        return False
    return True


def looks_like_explicit_done_update(raw_text: str) -> bool:
    if _NEGATED_DONE.search(raw_text) or _NEGATED_NOT_STARTED.search(raw_text):
        return False
    return bool(_EXPLICIT_DONE.search(raw_text)) and len(raw_text) < 220


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_context_update(raw_text: str, db: AsyncSession) -> str:
    updates = await extract_status_updates(raw_text, db)
    agenda_blocks = await agenda_manager.capture_agenda_from_text(raw_text, db)
    if not updates and not agenda_blocks:
        await capture_context_note(raw_text, db)
        return (
            "Entendi como atualização de contexto. Registrei isso sem marcar nenhuma tarefa como concluída.\n"
            "Se quiser, me pede depois: *minhas tarefas ativas*."
        )
    if not updates and agenda_blocks:
        return agenda_capture_response(agenda_blocks)

    applied_lines: list[str] = []
    unclear_lines: list[str] = []

    for upd in updates:
        task = upd.get("task")
        title = upd.get("title")
        status = upd["status"]
        note = upd.get("note")
        estimated_minutes = upd.get("estimated_minutes")
        category = upd.get("category", "work")

        if not task and title:
            mapped_status = map_status_to_task_status(status)
            task = await task_manager.upsert_task_from_context(
                title, db,
                status=mapped_status,
                category=category,
                note=note,
                estimated_minutes=estimated_minutes,
            )
            if category != "system":
                applied_lines.append(f"- {task.title} → {status_label(mapped_status)}")
            continue

        if not task:
            unclear_lines.append(f"- não consegui ligar com segurança: {upd['source'][:90]}")
            continue

        mapped_status = map_status_to_task_status(status)
        updated_task = await task_manager.update_task_status(task, mapped_status, db, note=note, category=category)
        if category != "system":
            applied_lines.append(f"- {updated_task.title} → {status_label(mapped_status)}")

    if not applied_lines and unclear_lines:
        return "Entendi como atualização de contexto, mas não apliquei nada com segurança:\n" + "\n".join(unclear_lines[:5])

    response = ["Atualizei seu estado atual assim:"]
    response.extend(applied_lines[:8])
    if agenda_blocks:
        response.append("\nAgenda registrada:")
        response.extend([f"- {format_agenda_block(block)}" for block in agenda_blocks[:5]])
    if unclear_lines:
        response.append("\nPontos que deixei sem aplicar automaticamente:")
        response.extend(unclear_lines[:4])
    response.append("\nAgora, quando você pedir *minhas tarefas ativas*, eu vou considerar esse estado novo.")
    return "\n".join(response)


async def handle_explicit_done_update(raw_text: str, db: AsyncSession) -> str:
    updates = await extract_status_updates(raw_text, db)
    if not updates:
        return "Entendi como atualização, mas não consegui ligar isso com segurança a uma tarefa ativa."

    lines = []
    for upd in updates:
        task = upd.get("task")
        title = upd.get("title")
        status = upd["status"]
        note = upd.get("note")
        estimated_minutes = upd.get("estimated_minutes")
        category = upd.get("category", "work")

        if status == "done":
            if task:
                done_task, _ = await task_manager.mark_done(task.title, db)
                if done_task and category != "system":
                    lines.append(f"- {done_task.title} → concluída")
                    continue
            if title:
                created = await task_manager.upsert_task_from_context(
                    title, db, status="done", category=category,
                    note=note, estimated_minutes=estimated_minutes,
                )
                if category != "system":
                    lines.append(f"- {created.title} → concluída")
                continue
        else:
            mapped_status = map_status_to_task_status(status)
            if task:
                updated_task = await task_manager.update_task_status(task, mapped_status, db, note=note, category=category)
                if category != "system":
                    lines.append(f"- {updated_task.title} → {status_label(mapped_status)}")
                continue
            if title:
                created = await task_manager.upsert_task_from_context(
                    title, db, status=mapped_status, category=category,
                    note=note, estimated_minutes=estimated_minutes,
                )
                if category != "system":
                    lines.append(f"- {created.title} → {status_label(mapped_status)}")
                continue

    if not lines:
        return "Entendi a intenção, mas não consegui aplicar nada com segurança."
    return "Atualização aplicada:\n" + "\n".join(lines)


async def extract_status_updates(raw_text: str, db: AsyncSession) -> list[dict]:
    chunks = split_update_chunks(raw_text)
    active_tasks = list(await task_manager.get_active_tasks(db, include_system=True))
    recent_tasks = list(await task_manager.get_recent_tasks(db, limit=80, include_system=True))
    all_tasks = active_tasks + [t for t in recent_tasks if t not in active_tasks]
    updates: list[dict] = []
    seen_keys: set[str] = set()
    anchor_title: str | None = None
    anchor_task = None
    anchor_category = "work"
    anchor_status = "pending"

    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped or should_skip_chunk(stripped):
            continue

        category = infer_category(stripped)
        status = detect_status(stripped)
        title = extract_title_candidate(stripped)
        note = extract_note(stripped)
        estimated_minutes = extract_estimated_minutes(stripped)
        task = match_task_for_chunk(stripped, title, all_tasks, include_system=(category == "system"))
        is_field_line = looks_like_field_line(stripped)
        is_section_header = looks_like_section_header(stripped)

        if is_section_header:
            continue

        if title and not status and not is_field_line:
            if category != "system":
                anchor_title = title
                anchor_task = task
                anchor_category = category
                anchor_status = "pending"
            continue

        if is_field_line and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            fn = field_line_note(stripped)
            note = f"{note} | {fn}" if note and fn and fn not in note else (fn or note)
            status = status or anchor_status
            category = anchor_category

        if _NOTE_ONLY_HINTS.search(stripped) and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            if status is None:
                status = anchor_status or "in_progress"
            category = anchor_category

        if is_note_only_candidate(title) and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            if status is None:
                status = anchor_status or "in_progress"
            category = anchor_category

        if status is None:
            continue

        if task is None and title is None and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            category = anchor_category

        if task or title:
            if category != "system":
                anchor_task = task
                anchor_title = task.title if task else task_manager.canonicalize_task_title(title)
                anchor_category = category
                anchor_status = status

        dedupe = f"{(task.title if task else title) or stripped}:{status}:{category}:{note or ''}"
        if dedupe in seen_keys:
            continue
        seen_keys.add(dedupe)
        updates.append({
            "task": task,
            "title": task_manager.canonicalize_task_title(title) if title else None,
            "status": status,
            "note": note,
            "estimated_minutes": estimated_minutes,
            "category": category,
            "source": stripped,
        })
    return updates
