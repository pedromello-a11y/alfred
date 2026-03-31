from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Message
from app.services import alfred_brain_unified

router = APIRouter(prefix="/internal/whatsapp", tags=["internal-whatsapp"])


class GatewayInboundPayload(BaseModel):
    text: str
    chat_id: str
    message_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    source: str = "whatsapp-web.js"
    from_me: bool = False
    is_group: bool = False


@router.post("/inbound")
async def inbound_from_gateway(
    payload: GatewayInboundPayload,
    db: AsyncSession = Depends(get_db),
    x_bridge_secret: str | None = Header(default=None, alias="X-Bridge-Secret"),
):
    expected_secret = (settings.wa_bridge_shared_secret or "").strip()
    if expected_secret and x_bridge_secret != expected_secret:
        raise HTTPException(status_code=401, detail="invalid_bridge_secret")

    text_body = (payload.text or "").strip()
    if not text_body:
        raise HTTPException(status_code=400, detail="empty_text")

    gateway_message_id = f"waweb:{payload.message_id}" if payload.message_id else None
    if gateway_message_id:
        existing = await db.execute(select(Message).where(Message.whapi_id == gateway_message_id))
        if existing.scalar_one_or_none() is not None:
            return {"status": "duplicate", "reply": None}

    inbound = Message(
        direction="inbound",
        content=text_body,
        message_type="text",
        processed=False,
        whapi_id=gateway_message_id,
    )
    db.add(inbound)
    await db.flush()

    try:
        response_text, classification = await alfred_brain_unified.process_message(
            text_body,
            db,
            origin="whatsapp",
        )
        inbound.processed = True
        inbound.classification = classification
        db.add(
            Message(
                direction="outbound",
                content=response_text,
                message_type="text",
                processed=True,
            )
        )
        await db.commit()
        return {
            "status": "ok",
            "reply": response_text,
            "classification": classification,
        }
    except Exception as exc:
        inbound.processed = False
        inbound.classification = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"inbound_processing_failed: {exc}")
