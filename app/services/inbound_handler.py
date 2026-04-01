"""Ponto único de processamento de mensagens inbound.

Todos os routers (webhook, whatsapp, internal_whatsapp) chamam este service.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message
from app.services import alfred_brain_v2


async def process_inbound(
    text: str,
    db: AsyncSession,
    *,
    origin: str = "whatsapp",
    message_id: str | None = None,
) -> dict:
    """Processa uma mensagem inbound de ponta a ponta.

    Returns:
        {
            "status": "ok" | "duplicate" | "ignored" | "error",
            "reply": str | None,
            "classification": str | None,
        }
    """
    text = (text or "").strip()
    if not text:
        return {"status": "ignored", "reply": None, "classification": None}

    if message_id:
        existing = await db.execute(select(Message).where(Message.whapi_id == message_id))
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
        reply_text, classification = await alfred_brain_v2.process_message(text, db, origin=origin)
        inbound.processed = True
        inbound.classification = classification
        db.add(Message(direction="outbound", content=reply_text, message_type="text", processed=True))
        await db.commit()
        return {"status": "ok", "reply": reply_text, "classification": classification}
    except Exception as exc:
        inbound.processed = False
        inbound.classification = "error"
        await db.commit()
        return {"status": "error", "reply": None, "classification": "error"}
