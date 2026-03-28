import httpx
from loguru import logger

from app.config import settings


async def send_message(phone: str, text: str) -> dict:
    """Envia mensagem de texto via Whapi."""
    async with httpx.AsyncClient() as client:
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
        resp.raise_for_status()
        logger.debug("Message sent to {}", phone)
        return resp.json()
