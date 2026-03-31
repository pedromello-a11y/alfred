from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock, DumpItem, Task
from app.services import agenda_manager, dump_manager, interpreter, message_handler, task_manager

_PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}


async def _remember_last_action(kind: str, object_id: str, db: AsyncSession) -> None:
    await task_manager.set_setting("last_action_type", kind, db)
    await task_manager.set_setting("last_action_id", object_id, db)
    if kind == "task":
        await task_manager.set_setting("last_created_task_id", object_id, db)


def _compose_title(task_title: str, project: str | None = None) -> str:
    title = (task_title or "").strip()
    project_name = (project or "").strip()
    if project_name and title:
        return f"{project_name} | {title}"
    return title or project_name


def _to_naive_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


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
        result = await db.execute(
            select(DumpItem)
            .where((DumpItem.rewritten_title.ilike(f"%{reference}%")) | (DumpItem.raw_text.ilike(f"%{reference}%")))
            .order_by(DumpItem.created_at.desc())
            .limit(1)
        )
        dump = result.scalar_one_or_none()
        if dump:
            return dump
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
        result = await db.execute(
            select(AgendaBlock)
            .where(AgendaBlock.title.ilike(f"%{reference}%"))
            .order_by(AgendaBlock.start_at.desc())
            .limit(1)
        )
        block = result.scalar_one_or_none()
        if block:
            return block
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
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            target = active[0]
            target.notes = f"{target.notes or ''}\n[{timestamp}] {note_text}".strip()
            await db.commit()
            await _remember_last_action("task", str(target.id), db)
            return target, f"Corrigido. Removi a task e guardei isso como nota em *{target.title}*."
        await db.commit()
        await task_manager.set_setting("last_context_note", note_text, db)
        await _remember_last_action("note", "context", db)
        return None, "Corrigido. Removi a task e guardei isso como nota contextual."

    return None, "Entendi como correção, mas essa conversão ainda não está suportada automaticamente."


def _agenda_response(blocks: list[AgendaBlock]) -> str:
    lines = ["Agenda registrada:"]
    for block in blocks[:5]:
        lines.append(f"- {block.title} — {block.start_at.strftime('%d/%m %H:%M')}→{block.end_at.strftime('%H:%M')}")
    return "\n".join(lines)


async def handle(raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None):
    if db is None:
        return await message_handler.handle(raw_text, origin=origin, db=db)

    decision = await interpreter.interpret_message(raw_text, db)
    if not decision or decision.get("confidence", 0) < 0.6:
        return await message_handler.handle(raw_text, origin=origin, db=db)

    intent = decision.get("intent")
    if intent == "new_task":
        task = await _create_task_from_interpretation(decision, origin, db)
        item = message_handler.InboundItem(item_type="task", origin=origin, raw_text=raw_text, extracted_title=task.title)
        return item, f"Anotado: *{task.title}*.", "new_task"

    if intent == "dump":
        dump = await dump_manager.create_dump_item(raw_text, origin, db)
        await _remember_last_action("dump", str(dump.id), db)
        item = message_handler.InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=dump.rewritten_title)
        return item, f"Registrado em dumps como *{dump.rewritten_title}* ({dump.category or 'desconhecido'}).", "dump"

    if intent == "agenda_add":
        blocks = await _persist_blocks_from_interpretation(decision, origin, db)
        if blocks:
            item = message_handler.InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title=blocks[-1].title)
            return item, _agenda_response(blocks), "agenda_add"
        return await message_handler.handle(raw_text, origin=origin, db=db)

    if intent == "task_update":
        task = await _update_task_from_interpretation(decision, db)
        if task:
            item = message_handler.InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title=task.title)
            return item, f"Atualizado: *{task.title}* → {task.status}.", "task_update"
        return await message_handler.handle(raw_text, origin=origin, db=db)

    if intent == "correction":
        obj, response = await _apply_correction(decision, origin, db)
        item = message_handler.InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title=decision.get("correction_new_type") or "correction")
        return item, response, "correction"

    if intent in {"question", "chat", "unknown"}:
        return await message_handler.handle(raw_text, origin=origin, db=db)

    return await message_handler.handle(raw_text, origin=origin, db=db)
