"""Router do webhook Whapi — sem autenticação por design."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Message
from app.services import whapi_client
from app.services.inbound_handler import process_inbound

router = APIRouter()


def _should_accept_self_authored(msg: dict) -> bool:
    source = (msg.get("source") or "").lower()
    chat_id = str(msg.get("chat_id") or "")
    return source in {"web", "mobile"} and chat_id.endswith("@g.us")


async def _is_recent_echo(text: str, db: AsyncSession, window: int = 120) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
    result = await db.execute(
        select(Message)
        .where(Message.direction == "outbound")
        .where(Message.content == text)
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
        if msg.get("type", "") != "text":
            continue
        text_body = (msg.get("text") or {}).get("body", "").strip()
        if not text_body:
            continue

        is_from_me = bool(msg.get("from_me"))
        if is_from_me:
            if not _should_accept_self_authored(msg):
                continue
            if await _is_recent_echo(text_body, db):
                continue
        else:
            sender = msg.get("from", "").split("@")[0]
            if sender != settings.pedro_phone:
                logger.warning("Ignored msg from unknown sender: {}", sender)
                continue

        whapi_id = msg.get("id")
        result = await process_inbound(
            text_body, db, origin="whatsapp", message_id=whapi_id
        )

        if result["status"] == "ok" and result["reply"]:
            try:
                await whapi_client.send_message(settings.pedro_phone, result["reply"])
            except Exception as exc:
                logger.error("Failed to send reply via whapi: {}", exc)

    return {"status": "ok"}
