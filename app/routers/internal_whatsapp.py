"""Router de inbound interno — gateway whatsapp-web.js."""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.inbound_handler import process_inbound

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
) -> dict:
    expected = (settings.wa_bridge_shared_secret or "").strip()
    if expected and x_bridge_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_bridge_secret")

    text_body = (payload.text or "").strip()
    if not text_body:
        raise HTTPException(status_code=400, detail="empty_text")

    gateway_id = f"waweb:{payload.message_id}" if payload.message_id else None

    result = await process_inbound(
        text_body, db, origin="whatsapp", message_id=gateway_id
    )

    if result["status"] == "error":
        raise HTTPException(status_code=500, detail="inbound_processing_failed")

    return result
