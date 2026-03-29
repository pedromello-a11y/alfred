import httpx
from loguru import logger

from app.config import settings


async def send_message(phone: str, text: str) -> dict:
    """Envia mensagem de texto via Whapi. Retorna dict com status='ok' ou status='failed'."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{settings.whapi_api_url}/messages/text",
                headers={
                    "Authorization": f"Bearer {settings.whapi_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": f"{phone}@s.whatsapp.net",
                    "body": text,
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "Whapi delivery failed: status={} body={}",
                    resp.status_code,
                    resp.text[:300],
                )
                return {"status": "failed", "error": resp.text, "http_status": resp.status_code}
            logger.debug("Message sent to {}", phone)
            return {"status": "ok", **resp.json()}
    except Exception as exc:
        logger.error("send_message exception: {}", exc)
        return {"status": "failed", "error": str(exc)}
