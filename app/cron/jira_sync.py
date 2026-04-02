"""Sincroniza issues do Jira para tasks locais."""
from loguru import logger

from app.database import AsyncSessionLocal


async def run() -> None:
    try:
        from app.services.jira_client import _is_configured, sync_issues_to_local
        if not _is_configured():
            return
        async with AsyncSessionLocal() as db:
            count = await sync_issues_to_local(db)
            logger.info("Jira sync: {} issues synced", count)
    except Exception as exc:
        logger.error("jira_sync.run failed: {}", exc)
