"""Script para arquivar dumps lixo: status=unknown e confidence < 0.4.

Uso:
    python -m scripts.cleanup_junk_dumps
"""
import asyncio

from loguru import logger
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import DumpItem


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DumpItem)
            .where(DumpItem.status == "unknown")
            .where((DumpItem.confidence == None) | (DumpItem.confidence < 0.4))
        )
        items = result.scalars().all()
        for item in items:
            item.status = "archived"
        await db.commit()
        logger.info("cleanup_junk_dumps: {} items archived", len(items))
        print(f"Arquivados: {len(items)} dumps lixo.")


if __name__ == "__main__":
    asyncio.run(main())
