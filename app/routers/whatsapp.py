"""Router de inbound WhatsApp via bridge gateway."""
from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.inbound_handler import process_inbound

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


class InboundPayload(BaseModel):
    text: str
    chat_id: str
    message_id: str | None = None
    source: str = "whatsapp"


def _is_allowed_chat(chat_id: str) -> bool:
    allowed = (settings.allowed_chat_id or "").strip()
    if not allowed:
        logger.warning(
            "ALLOWED_CHAT_ID não configurado — processando msg de chat_id='{}' sem filtro", chat_id
        )
        return True
    return chat_id.strip() == allowed


@router.post("/inbound")
async def inbound(
    payload: InboundPayload,
    db: AsyncSession = Depends(get_db),
    x_bridge_secret: str | None = Header(default=None, alias="X-Bridge-Secret"),
) -> dict:
    expected = (settings.wa_bridge_shared_secret or "").strip()
    if expected and x_bridge_secret != expected:
        raise HTTPException(status_code=401, detail="invalid_secret")

    logger.info(
        "whatsapp inbound: chat_id='{}' text_len={}",
        payload.chat_id,
        len(payload.text or ""),
    )

    if not _is_allowed_chat(payload.chat_id):
        logger.debug("ignorando mensagem de chat não permitido: '{}'", payload.chat_id)
        return {"status": "ignored", "reason": "chat_not_allowed"}

    result = await process_inbound(
        payload.text,
        db,
        origin=payload.source or "whatsapp",
        message_id=payload.message_id,
    )

    if result["status"] == "ignored":
        return {"status": "ignored", "reason": "empty"}
    if result["status"] == "duplicate":
        return {"status": "ignored", "reason": "duplicate"}

    return result
