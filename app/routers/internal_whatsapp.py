"""Router de inbound interno — gateway whatsapp-web.js."""
from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
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


def _is_allowed_chat(chat_id: str) -> bool:
    allowed = (settings.allowed_chat_id or "").strip()
    if not allowed:
        logger.warning(
            "ALLOWED_CHAT_ID não configurado — processando msg de chat_id='{}' sem filtro", chat_id
        )
        return True
    return chat_id.strip() == allowed


@router.post("/inbound")
async def inbound_from_gateway(
    payload: GatewayInboundPayload,
    db: AsyncSession = Depends(get_db),
    x_bridge_secret: str | None = Header(default=None, alias="X-Bridge-Secret"),
) -> dict:
    expected = (settings.wa_bridge_shared_secret or "").strip()
    if expected and x_bridge_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_bridge_secret")

    logger.info(
        "gateway inbound: chat_id='{}' from_me={} is_group={} text_len={}",
        payload.chat_id,
        payload.from_me,
        payload.is_group,
        len(payload.text or ""),
    )

    if not _is_allowed_chat(payload.chat_id):
        logger.debug("ignorando mensagem de chat não permitido: '{}'", payload.chat_id)
        return {"status": "ignored", "reason": "chat_not_allowed"}

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
