"""
jira_sync.py — a cada 2h seg-sex
Sincroniza issues do Jira com jira_cache local.
Se houver issues com status changed: notifica Pedro.
"""
from loguru import logger

from app.config import settings
from app.database import AsyncSessionLocal
from app.services import jira_client, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            result = await jira_client.sync_to_cache(db)
            logger.info(
                "Jira sync done. new={}, updated={}, total={}",
                result["new"], result["updated"], result["total"],
            )
            if result["updated"] > 0:
                msg = (
                    f"Jira atualizado: {result['updated']} issue(s) mudaram de status. "
                    f"Use 'o que tenho no Jira?' pra ver."
                )
                await whapi_client.send_message(settings.pedro_phone, msg)
    except Exception as exc:
        logger.error("jira_sync.run failed: {}", exc)
