"""Ponto único de processamento de mensagens inbound.

Todos os routers (webhook, whatsapp, internal_whatsapp) chamam este service.
"""
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.services import runtime_router
from app.services.focus_snapshot import build_focus_snapshot


def _build_operational_tail(snapshot: dict, classification: str) -> str:
    """Adiciona contexto operacional (bloco atual / próximo foco) à resposta."""
    if classification == "question":
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
