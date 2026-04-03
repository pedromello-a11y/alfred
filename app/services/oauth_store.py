"""Persistência de OAuth tokens no banco de dados."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OAuthToken


async def get_google_token(db: AsyncSession) -> OAuthToken | None:
    result = await db.execute(
        select(OAuthToken).where(OAuthToken.provider == "google")
    )
    return result.scalar_one_or_none()


async def save_google_token(refresh_token: str, db: AsyncSession) -> OAuthToken:
    row = await get_google_token(db)
    if row:
        row.refresh_token = refresh_token
        row.is_valid = True
    else:
        row = OAuthToken(provider="google", refresh_token=refresh_token, is_valid=True)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def invalidate_google_token(db: AsyncSession) -> None:
    row = await get_google_token(db)
    if row:
        row.is_valid = False
        await db.commit()
