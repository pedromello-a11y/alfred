from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Message
from app.services import message_handler, whapi_client

router = APIRouter()


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    messages = payload.get("messages", [])

    for msg in messages:
        # Ignorar mensagens próprias (evitar loop)
        if msg.get("from_me"):
            continue

        # Ignorar mensagens de outros números (segurança)
        sender = msg.get("from", "").split("@")[0]
        if sender != settings.pedro_phone:
            logger.warning("Ignored message from unknown sender: {}", sender)
            continue

        msg_type = msg.get("type", "")
        if msg_type != "text":
            logger.info("Ignored non-text message type: {}", msg_type)
            continue

        text_body = (msg.get("text") or {}).get("body", "").strip()
        if not text_body:
            continue

        # C2 — Deduplicação: checar whapi_id antes de processar
        whapi_id = msg.get("id")
        if whapi_id:
            existing = await db.execute(
                select(Message).where(Message.whapi_id == whapi_id)
            )
            if existing.scalar_one_or_none():
                logger.info("Duplicate message skipped: whapi_id={}", whapi_id)
                continue

        # Persistir mensagem inbound imediatamente (antes do processamento)
        inbound = Message(
            direction="inbound",
            content=text_body,
            message_type="text",
            processed=False,
            whapi_id=whapi_id,
        )
        db.add(inbound)
        await db.flush()  # garante que whapi_id foi persistido antes de processar

        # C3 — Wrap em try/except: erro não perde a mensagem
        try:
            item, response_text, classification = await message_handler.handle(
                text_body, origin="whatsapp", db=db
            )

            inbound.processed = True
            inbound.classification = classification

            outbound = Message(
                direction="outbound",
                content=response_text,
                message_type="text",
                processed=True,
            )
            db.add(outbound)
            await db.commit()

            await whapi_client.send_message(settings.pedro_phone, response_text)

        except Exception as exc:
            logger.error("Webhook processing failed for msg '{}': {}", text_body[:80], exc)
            inbound.processed = False
            inbound.classification = "error"
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            try:
                await whapi_client.send_message(
                    settings.pedro_phone,
                    "Dificuldade técnica, tenta de novo em 5min."
                )
            except Exception:
                pass

    return {"status": "ok"}
