from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Message
from app.services import alfred_brain_unified, whapi_client

router = APIRouter()


def _should_accept_self_authored_message(msg: dict) -> bool:
    source = (msg.get("source") or "").lower()
    chat_id = str(msg.get("chat_id") or "")
    return source in {"web", "mobile"} and chat_id.endswith("@g.us")


async def _is_recent_outbound_echo(text_body: str, db: AsyncSession, window_seconds: int = 120) -> bool:
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    result = await db.execute(
        select(Message)
        .where(Message.direction == "outbound")
        .where(Message.content == text_body)
        .where(Message.created_at >= cutoff)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


@router.post("/webhook")
async def webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()

    messages = payload.get("messages", [])
    if not messages and isinstance(payload.get("message"), dict):
        messages = [payload["message"]]

    for msg in messages:
        msg_type = msg.get("type", "")
        if msg_type != "text":
            logger.info("Ignored non-text message type: {}", msg_type)
            continue

        text_body = (msg.get("text") or {}).get("body", "").strip()
        if not text_body:
            logger.info("Ignored empty text body.")
            continue

        whapi_id = msg.get("id")
        if whapi_id:
            existing = await db.execute(select(Message).where(Message.whapi_id == whapi_id))
            if existing.scalar_one_or_none():
                logger.info("Duplicate message skipped: whapi_id={}", whapi_id)
                continue

        is_from_me = bool(msg.get("from_me"))
        if is_from_me:
            if not _should_accept_self_authored_message(msg):
                logger.info("Ignored self-authored message: source={} chat_id={}", msg.get("source"), msg.get("chat_id"))
                continue
            if await _is_recent_outbound_echo(text_body, db):
                logger.info("Ignored self-authored echo of recent outbound message: {}", text_body[:80])
                continue
        else:
            sender = msg.get("from", "").split("@")[0]
            if sender != settings.pedro_phone:
                logger.warning("Ignored message from unknown sender: {}", sender)
                continue

        inbound = Message(direction="inbound", content=text_body, message_type="text", processed=False, whapi_id=whapi_id)
        db.add(inbound)
        await db.flush()

        try:
            response_text, classification = await alfred_brain_unified.process_message(text_body, db, origin="whatsapp")
            inbound.processed = True
            inbound.classification = classification
            db.add(Message(direction="outbound", content=response_text, message_type="text", processed=True))
            await db.commit()
            await whapi_client.send_message(settings.pedro_phone, response_text)
        except Exception as exc:
            logger.error("Unified webhook processing failed for msg '{}': {}", text_body[:80], exc)
            inbound.processed = False
            inbound.classification = "error"
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            try:
                await whapi_client.send_message(settings.pedro_phone, "Dificuldade técnica, tenta de novo em 5min.")
            except Exception:
                pass

    return {"status": "ok"}
