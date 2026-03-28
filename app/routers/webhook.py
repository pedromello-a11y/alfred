from fastapi import APIRouter, Depends, Request
from loguru import logger
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

        # Normalizar para InboundItem e obter classificação + resposta
        item, response_text, classification = await message_handler.handle(text_body, origin="whatsapp")

        # Persistir mensagem inbound com classificação
        inbound = Message(
            direction="inbound",
            content=text_body,
            message_type="text",
            processed=True,
            classification=classification,
        )
        db.add(inbound)

        # Persistir resposta outbound
        outbound = Message(
            direction="outbound",
            content=response_text,
            message_type="text",
            processed=True,
        )
        db.add(outbound)
        await db.commit()

        # Enviar resposta via Whapi
        await whapi_client.send_message(settings.pedro_phone, response_text)

    return {"status": "ok"}
