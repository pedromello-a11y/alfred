from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Message
from app.services import alfred_brain_unified

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


class InboundPayload(BaseModel):
    text: str
    chat_id: str
    message_id: str | None = None
    source: str = "whatsapp"


@router.post("/inbound")
async def inbound(
    payload: InboundPayload,
    db: AsyncSession = Depends(get_db),
    x_bridge_secret: str | None = Header(default=None, alias="X-Bridge-Secret"),
):
    expected = (settings.wa_bridge_shared_secret or "").strip()
    if expected and x_bridge_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_secret")

    text = (payload.text or "").strip()
    if not text:
        return {"status": "ignored", "reason": "empty"}

    if payload.message_id:
        existing = await db.execute(
            select(Message).where(Message.whapi_id == payload.message_id)
        )
        if existing.scalar_one_or_none():
            return {"status": "ignored", "reason": "duplicate"}

    inbound_msg = Message(
        direction="inbound",
        content=text,
        message_type="text",
        processed=False,
        whapi_id=payload.message_id,
    )
    db.add(inbound_msg)
    await db.flush()

    try:
        reply_text, classification = await alfred_brain_unified.process_message(
            text,
            db,
            origin=payload.source or "whatsapp",
        )
        inbound_msg.processed = True
        inbound_msg.classification = classification
        db.add(
            Message(
                direction="outbound",
                content=reply_text,
                message_type="text",
                processed=True,
            )
        )
        await db.commit()
        return {"status": "ok", "reply": reply_text, "classification": classification}
    except Exception:
        inbound_msg.processed = False
        inbound_msg.classification = "error"
        await db.commit()
        return {"status": "error", "reply": "Dificuldade técnica, tenta de novo em 5min."}
