"""
Utilitários compartilhados entre handlers:
- _build_context / _build_jira_active_lines
- _append_task_note / _capture_context_note
- _maybe_grant_rest_xp
- formatadores de agenda e status
"""
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_manager


async def build_jira_active_lines(db: AsyncSession) -> list[str]:
    try:
        from app.services.jira_client import _is_configured, build_active_lines
        if not _is_configured():
            return []
        return await build_active_lines(db)
    except Exception:
        return []


async def build_context(db: AsyncSession | None) -> str:
    if db is None:
        return "(sem contexto disponível)"
    tasks = await task_manager.get_active_tasks(db)
    if tasks:
        lines = [
            f"- {t.title} (status {t.status}, prioridade {t.priority or '-'}, prazo {t.deadline or 'sem prazo'})"
            for t in tasks[:10]
        ]
        return "Tarefas ativas:\n" + "\n".join(lines)
    jira_lines = await build_jira_active_lines(db)
    if jira_lines:
        return "Demandas ativas do Jira/cache:\n" + "\n".join(jira_lines)
    return "Nenhuma tarefa ativa."


async def append_task_note(task, raw_text: str, db: AsyncSession) -> None:
    snippet = raw_text[:300].strip()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    addition = f"[{now_str}] {snippet}"
    task.notes = f"{task.notes}\n{addition}" if task.notes else addition
    await db.commit()
    logger.debug("Context note appended to task {}", task.id)


async def capture_context_note(raw_text: str, db: AsyncSession) -> None:
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return
    raw_lower = raw_text.lower()
    matched_task = None
    for t in tasks[:10]:
        title_words = [w for w in (t.title or "").lower().split() if len(w) > 3]
        if title_words and any(w in raw_lower for w in title_words):
            matched_task = t
            break
    if matched_task:
        await append_task_note(matched_task, raw_text, db)


async def maybe_grant_rest_xp(raw_text: str, db: AsyncSession) -> None:
    from sqlalchemy import select as _select
    from app.models import Message as _Message

    pause_keywords = ("pausa", "descanso", "coffee break", "descanse", "respira", "break")
    last_out = await db.execute(
        _select(_Message).where(_Message.direction == "outbound").order_by(_Message.created_at.desc()).limit(1)
    )
    last_outbound = last_out.scalar_one_or_none()
    if not last_outbound or not any(kw in last_outbound.content.lower() for kw in pause_keywords):
        return
    stat = await task_manager.grant_xp("recovery", 10, db)
    await task_manager.set_setting("rest_xp_granted_today", "true", db)
    logger.info("Rest XP granted mid-conversation: +10 recovery (nível {})", stat.level)


# ── Formatadores ──────────────────────────────────────────────────────────────

def format_agenda_block(block) -> str:
    return f"{block.title} — {block.start_at.strftime('%d/%m %H:%M')}→{block.end_at.strftime('%H:%M')}"


def agenda_blocks_inline(blocks) -> str:
    return "; ".join(format_agenda_block(block) for block in blocks[:3])


def agenda_capture_response(blocks) -> str:
    lines = ["Agenda registrada:"]
    lines.extend([f"- {format_agenda_block(block)}" for block in blocks[:5]])
    return "\n".join(lines)


def status_label(status: str) -> str:
    return {
        "done": "concluída",
        "in_progress": "em andamento",
        "pending": "pendente",
        "delegated": "delegada",
        "dropped": "removida",
    }.get(status, status)


def map_status_to_task_status(status: str) -> str:
    if status == "done":
        return "done"
    if status == "in_progress":
        return "in_progress"
    return "pending"
