"""Serviço de settings — CRUD na tabela Settings."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Settings


async def get_setting(key: str, default: str | None = None, db: AsyncSession | None = None) -> str | None:
    if db is None:
        return default
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def set_setting(key: str, value: str, db: AsyncSession) -> None:
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        db.add(Settings(key=key, value=value))
    else:
        setting.value = value
    await db.commit()
