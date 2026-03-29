import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Message
from app.services import message_handler

router = APIRouter()


class WhatsAppBridgeInbound(BaseModel):
    text: str
    chat_id: str
    message_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    source: str = "whatsapp-web.js"
    from_me: bool = False
    is_group: Optional[bool] = None


async def _validate_secret(x_bridge_secret: str | None = Header(default=None)) -> None:
    expected = (os.getenv("WA_BRIDGE_SHARED_SECRET") or "").strip()
    if expected and x_bridge_secret != expected:
        raise HTTPException(status_code=401, detail="invalid bridge secret")


@router.get("/internal/whatsapp/ping")
async def whatsapp_bridge_ping(_: None = Depends(_validate_secret)):
    return {"status": "ok"}


@router.post("/internal/whatsapp/inbound")
async def whatsapp_bridge_inbound(
    payload: WhatsAppBridgeInbound,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_validate_secret),
):
    text_body = payload.text.strip()
    if not text_body:
        return {"status": "ignored", "reason": "empty_text"}

    if payload.message_id:
        existing = await db.execute(
            select(Message).where(Message.whapi_id == payload.message_id)
        )
        if existing.scalar_one_or_none():
            logger.info("WA bridge duplicate skipped: message_id={}", payload.message_id)
            return {"status": "ignored", "reason": "duplicate"}

    inbound = Message(
        direction="inbound",
        content=text_body,
        message_type="text",
        processed=False,
        whapi_id=payload.message_id,
    )
    db.add(inbound)
    await db.flush()

    try:
        item, response_text, classification = await message_handler.handle(
            text_body,
            origin="whatsapp",
            db=db,
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

        return {
            "status": "ok",
            "reply": response_text,
            "classification": classification,
            "chat_id": payload.chat_id,
        }
    except Exception as exc:
        logger.error("WA bridge inbound failed for '{}': {}", text_body[:100], exc)
        inbound.processed = False
        inbound.classification = "error"
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        return {
            "status": "error",
            "reply": "Dificuldade técnica, tenta de novo em 5min.",
            "chat_id": payload.chat_id,
        }
