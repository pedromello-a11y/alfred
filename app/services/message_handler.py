from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import brain, task_manager


# ---------------------------------------------------------------------------
# InboundItem — formato padrão de entrada para todos os canais
# ---------------------------------------------------------------------------

@dataclass
class InboundItem:
    item_type: str              # 'task', 'reminder', 'idea', 'event', 'update'
    origin: str                 # 'whatsapp', 'jira', 'gcal', 'gmail'
    raw_text: str               # texto original
    extracted_title: str        # título extraído
    deadline: Optional[date] = None
    priority_hint: Optional[str] = None  # 'high', 'medium', 'low'
    category: Optional[str] = None       # 'work', 'personal'
    needs_confirmation: bool = False
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


_ITEM_TYPE_MAP = {
    "new_task": "task",
    "update":   "update",
    "question": "task",
    "command":  "task",
    "chat":     "idea",
}


# ---------------------------------------------------------------------------
# handle — classifica via brain, normaliza para InboundItem, roteia
# Retorna (item, response_text, classification)
# ---------------------------------------------------------------------------

async def handle(
    raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None
) -> tuple["InboundItem", str, str]:
    """Classifica via Claude Haiku, roteia e retorna (item, response, classification)."""
    data = await brain.classify(raw_text, db=db)
    classification = data.get("classification", "chat")
    logger.info("Classification: {}", classification)

    deadline_raw = data.get("extracted_deadline")
    deadline: Optional[date] = None
    if deadline_raw:
        try:
            deadline = date.fromisoformat(str(deadline_raw))
        except ValueError:
            pass

    item = InboundItem(
        item_type=_ITEM_TYPE_MAP.get(classification, "idea"),
        origin=origin,
        raw_text=raw_text,
        extracted_title=data.get("extracted_title") or raw_text[:80],
        deadline=deadline,
        priority_hint=data.get("priority_hint"),
    )

    response_text = await _route(item, classification, db)
    return item, response_text, classification


async def _route(item: InboundItem, classification: str, db: AsyncSession | None) -> str:
    if classification == "new_task":
        if db is not None:
            task = await task_manager.create(item, db)
            return f"Anotado: *{task.title}*. Prioridade: {item.priority_hint or 'normal'}."
        return f"Anotado: *{item.extracted_title}*."

    elif classification == "update":
        if db is not None:
            task = await task_manager.mark_done(item.extracted_title, db)
            if task:
                return f"Show! ✅ *{task.title}* concluída."
        return f"Show! Tarefa '{item.extracted_title}' marcada como concluída."

    elif classification == "question":
        context = await _build_context(db)
        return await brain.answer_question(item.raw_text, context, db=db)

    elif classification == "command":
        context = await _build_context(db)
        return await brain.execute_command(item.raw_text, context, db=db)

    else:
        return await brain.casual_response(item.raw_text, db=db)


async def _build_context(db: AsyncSession | None) -> str:
    if db is None:
        return "(sem contexto disponível)"
    tasks = await task_manager.get_pending(db)
    if not tasks:
        return "Nenhuma tarefa pendente."
    lines = [f"- {t.title} (prioridade {t.priority or '-'}, prazo {t.deadline or 'sem prazo'})" for t in tasks[:10]]
    return "Tarefas pendentes:\n" + "\n".join(lines)
