import os

import httpx
from loguru import logger


def _gateway_base_url() -> str:
    return (os.getenv("WA_GATEWAY_URL") or "").rstrip("/")


def _shared_secret() -> str:
    return (os.getenv("WA_BRIDGE_SHARED_SECRET") or "").strip()


async def send_message(target: str, text: str) -> dict:
    """
    Envia mensagem para o gateway whatsapp-web.js.
    Mantém a mesma assinatura do whapi_client.send_message(phone, text)
    para permitir monkeypatch sem mexer no restante do Alfred.
    """
    base_url = _gateway_base_url()
    if not base_url:
        logger.warning("WA_GATEWAY_URL not configured; outbound via gateway skipped.")
        return {"status": "failed", "error": "WA_GATEWAY_URL not configured"}

    headers = {"Content-Type": "application/json"}
    secret = _shared_secret()
    if secret:
        headers["X-Bridge-Secret"] = secret

    payload = {
        "chatId": target,
        "text": text,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url}/send", headers=headers, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "WA gateway delivery failed: status={} body={}",
                    resp.status_code,
                    resp.text[:400],
                )
                return {
                    "status": "failed",
                    "http_status": resp.status_code,
                    "error": resp.text,
                }
            data = resp.json()
            logger.debug("WA gateway delivered message to {}", target)
            return {"status": "ok", **data}
    except Exception as exc:
        logger.error("wa_bridge_client.send_message exception: {}", exc)
        return {"status": "failed", "error": str(exc)}
