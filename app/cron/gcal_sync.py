"""Sincroniza eventos do Google Calendar para agenda_blocks."""
from loguru import logger

from app.database import AsyncSessionLocal


async def run() -> None:
    try:
        from app.services import gcal_client
        if not gcal_client._is_configured():
            return
        async with AsyncSessionLocal() as db:
            count = await gcal_client.sync_to_agenda_blocks(db)
            if count:
                logger.info("gcal_sync: {} blocks synced", count)
    except Exception as exc:
        logger.error("gcal_sync.run failed: {}", exc)
