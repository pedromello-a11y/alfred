from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from loguru import logger

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


# ---------------------------------------------------------------------------
# classify — stub heurístico por palavras-chave (sem chamada externa)
# Sessão 3 substituirá por chamada real ao Claude Haiku.
# ---------------------------------------------------------------------------

_NEW_TASK_HINTS = ("preciso", "lembrar", "adicionar", "criar tarefa", "anotar", "fazer")
_UPDATE_HINTS   = ("terminei", "fiz", "concluí", "feito", "pronto", "acabei")
_QUESTION_HINTS = ("o que tenho", "próxima tarefa", "agenda", "tarefas de hoje", "o que fazer")
_COMMAND_HINTS  = ("reagendar", "cancelar", "priorizar", "remover tarefa", "adiar")


def _stub_classify(text: str) -> dict:
    """Classificação heurística local — placeholder até Sessão 3."""
    lower = text.lower()
    if any(h in lower for h in _NEW_TASK_HINTS):
        classification = "new_task"
    elif any(h in lower for h in _UPDATE_HINTS):
        classification = "update"
    elif any(h in lower for h in _QUESTION_HINTS):
        classification = "question"
    elif any(h in lower for h in _COMMAND_HINTS):
        classification = "command"
    else:
        classification = "chat"
    return {
        "classification": classification,
        "extracted_title": text[:80],
        "extracted_deadline": None,
        "priority_hint": None,
    }


# ---------------------------------------------------------------------------
# handle — normaliza para InboundItem e roteia (stubs)
# Retorna (InboundItem, response_text) para que o webhook possa
# persistir a classification no banco.
# ---------------------------------------------------------------------------

async def handle(raw_text: str, origin: str = "whatsapp") -> tuple[InboundItem, str, str]:
    """Retorna (item, response_text, classification) para que o webhook persista a classification."""
    data = _stub_classify(raw_text)
    classification = data["classification"]
    logger.info("Classification (stub): {}", classification)

    item = InboundItem(
        item_type=_classification_to_item_type(classification),
        origin=origin,
        raw_text=raw_text,
        extracted_title=data["extracted_title"],
        priority_hint=data["priority_hint"],
    )

    response_text = _route(item, classification)
    return item, response_text, classification


def _classification_to_item_type(classification: str) -> str:
    mapping = {
        "new_task": "task",
        "update":   "update",
        "question": "task",
        "command":  "task",
        "chat":     "idea",
    }
    return mapping.get(classification, "idea")


def _route(item: InboundItem, classification: str) -> str:
    if classification == "new_task":
        # Stub — task_manager.create() será implementado na Sessão 3
        logger.info("STUB task_manager.create: title={}", item.extracted_title)
        return f"Anotado: {item.extracted_title}. Prioridade: {item.priority_hint or 'normal'}."
    elif classification == "update":
        # Stub — task_manager.update_status() será implementado na Sessão 3
        logger.info("STUB task_manager.update_status: title={}", item.extracted_title)
        return f"Show! Tarefa '{item.extracted_title}' marcada como concluída."
    else:
        # Stub — brain.py será implementado na Sessão 3
        logger.info("STUB brain.{}: msg={}", classification, item.raw_text[:50])
        return "Entendido! (funcionalidade em desenvolvimento)"
