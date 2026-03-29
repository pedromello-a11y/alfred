import httpx
from loguru import logger

from app.config import settings


def _gateway_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.wa_bridge_shared_secret:
        headers["X-Bridge-Secret"] = settings.wa_bridge_shared_secret
    return headers


async def _send_via_gateway(phone: str, text: str) -> dict:
    gateway_url = settings.wa_gateway_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{gateway_url}/send",
            headers=_gateway_headers(),
            json={
                "chatId": phone,
                "text": text,
            },
        )
        if resp.status_code != 200:
            logger.warning(
                "WA gateway delivery failed: status={} body={}",
                resp.status_code,
                resp.text[:300],
            )
            return {
                "status": "failed",
                "error": resp.text,
                "http_status": resp.status_code,
            }

        logger.debug("Message sent via gateway to {}", phone)
        return {"status": "ok", **resp.json()}


async def _send_via_whapi(phone: str, text: str) -> dict:
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
            return {
                "status": "failed",
                "error": resp.text,
                "http_status": resp.status_code,
            }

        logger.debug("Message sent via Whapi to {}", phone)
        return {"status": "ok", **resp.json()}


async def send_message(phone: str, text: str) -> dict:
    """Envia mensagem usando o gateway WhatsApp Web quando configurado; caso contrário, usa Whapi."""
    try:
        if settings.wa_gateway_url:
            return await _send_via_gateway(phone, text)

        if not settings.whapi_token:
            error = "whapi_token_missing_and_wa_gateway_url_not_configured"
            logger.error("send_message configuration error: {}", error)
            return {"status": "failed", "error": error}

        return await _send_via_whapi(phone, text)
    except Exception as exc:
        logger.error("send_message exception: {}", exc)
        return {"status": "failed", "error": str(exc)}
