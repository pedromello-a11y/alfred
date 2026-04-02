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


def _is_active_tasks_question(text: str) -> bool:
    cleaned = re.sub(r"[?!.,;:]", "", (text or "").strip().lower()).strip()
    direct_patterns = [
        "demandas ativas", "demandas ativa", "demanda ativa",
        "tarefas ativas", "tarefas ativa", "tarefa ativa",
        "atividades ativas", "atividades ativa", "atividade ativa",
        "atividades em aberto", "tarefas em aberto", "demandas em aberto",
        "atividades abertas", "tarefas abertas", "demandas abertas",
        "o que tenho pra fazer", "o que falta fazer", "o que falta",
        "que tenho em aberto", "que esta em aberto", "que está em aberto",
        "todas atividades", "todas as atividades", "todas as tarefas",
        "todas demandas", "todas as demandas",
        "me diga minhas demandas", "me diga minhas tarefas",
        "quais sao minhas", "quais são minhas",
        "minhas demandas", "minhas tarefas", "minhas atividades",
        "minha demandas", "minha tarefas",
    ]
    return any(pattern in cleaned for pattern in direct_patterns)


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
            classification = "command"
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
