"""Ponto único de processamento de mensagens inbound.

Todos os routers (webhook, whatsapp, internal_whatsapp) chamam este service.
"""
import re

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.services import runtime_router
from app.services.active_tasks_view import format_active_tasks_for_whatsapp, get_unified_active_view
from app.services.focus_snapshot import build_focus_snapshot

_TASK_NOUNS = (
    "tarefa", "tarefas", "demanda", "demandas", "atividade", "atividades",
    "prioridade", "prioridades", "pendencia", "pendencias", "pendência", "pendências",
)

_TASK_QUERY_HINTS = (
    "ativas", "ativa", "abertas", "aberta", "em aberto", "pendentes", "pendente",
    "agora", "hoje", "lista", "listar", "quais", "qual", "me diga", "me fala",
    "o que tenho", "o que falta", "o que preciso", "minhas", "meus", "resta",
    "restando", "sobrou", "sobrando", "prioridade", "prioridades",
)


def _normalize_text(text: str) -> str:
    return re.sub(r"\\s+", " ", re.sub(r"[^\\w\\s]", " ", (text or "").lower())).strip()


def _is_active_tasks_question(text: str) -> bool:
    cleaned = _normalize_text(text)
    if not cleaned:
        return False

    direct_patterns = [
        "demandas ativas", "demanda ativa", "tarefas ativas", "tarefa ativa",
        "atividades ativas", "atividade ativa", "demandas em aberto", "tarefas em aberto",
        "atividades em aberto", "demandas abertas", "tarefas abertas", "atividades abertas",
        "tarefas pendentes", "demandas pendentes", "atividades pendentes",
        "o que tenho pra fazer", "o que eu tenho pra fazer", "o que falta fazer",
        "o que falta", "o que ainda falta", "quais sao minhas tarefas", "quais são minhas tarefas",
        "quais sao minhas demandas", "quais são minhas demandas", "quais sao minhas atividades",
        "quais são minhas atividades", "minhas tarefas", "minhas demandas", "minhas atividades",
        "lista de tarefas", "lista de demandas", "lista de atividades",
        "me diga minhas tarefas", "me diga minhas demandas", "me diga minhas atividades",
        "me fala minhas tarefas", "me fala minhas demandas", "me fala minhas atividades",
        "prioridades abertas", "prioridades de hoje", "minhas prioridades",
    ]
    if any(pattern in cleaned for pattern in direct_patterns):
        return True

    has_task_noun = any(noun in cleaned for noun in _TASK_NOUNS)
    has_hint = any(hint in cleaned for hint in _TASK_QUERY_HINTS)
    return has_task_noun and has_hint


def _build_operational_tail(snapshot: dict, classification: str) -> str:
    """Adiciona contexto operacional (bloco atual / próximo foco) à resposta."""
    if classification in {"question", "command"}:
        return ""
    current_block = snapshot.get("currentBlock") or {}
    suggestion = snapshot.get("suggestion") or {}
    if current_block.get("title"):
        return f"\n\nAgora: *{current_block['title']}* até {current_block.get('end', '')}."
    if suggestion.get("title"):
        return f"\n\nPróximo foco: *{suggestion['title']}* ({suggestion.get('reason', '')})."
    return ""


async def process_inbound(
    text: str,
    db: AsyncSession,
    *,
    origin: str = "whatsapp",
    message_id: str | None = None,
) -> dict:
    """Processa uma mensagem inbound de ponta a ponta.

    Returns:
        dict com keys: status, reply, classification
    """
    text = (text or "").strip()
    if not text:
        return {"status": "ignored", "reply": None, "classification": None}

    if message_id:
        existing = await db.execute(
            select(Message).where(Message.whapi_id == message_id)
        )
        if existing.scalar_one_or_none() is not None:
            return {"status": "duplicate", "reply": None, "classification": None}

    inbound = Message(
        direction="inbound",
        content=text,
        message_type="text",
        processed=False,
        whapi_id=message_id,
    )
    db.add(inbound)
    await db.flush()

    try:
        if _is_active_tasks_question(text):
            response_text = format_active_tasks_for_whatsapp(await get_unified_active_view(db))
            classification = "question"
        else:
            _item, response_text, classification = await runtime_router.handle(
                text, origin=origin, db=db
            )

        base = (response_text or "Entendi.").strip()
        cls = (classification or "unknown").strip()

        try:
            snapshot = await build_focus_snapshot(db)
            tail = _build_operational_tail(snapshot, cls)
        except Exception:
            tail = ""

        final_reply = (base + tail).strip()

        inbound.processed = True
        inbound.classification = cls
        db.add(Message(
            direction="outbound",
            content=final_reply,
            message_type="text",
            processed=True,
        ))
        await db.commit()
        return {"status": "ok", "reply": final_reply, "classification": cls}

    except Exception as exc:
        logger.error("inbound processing failed: {}", exc)
        inbound.processed = False
        inbound.classification = "error"
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        return {
            "status": "error",
            "reply": "Dificuldade técnica, tenta de novo em 5min.",
            "classification": "error",
        }
