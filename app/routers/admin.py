"""Endpoints administrativos — operações destrutivas protegidas por X-Admin-Key."""
from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import DumpItem

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    secret = (settings.admin_secret_key or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="ADMIN_SECRET_KEY não configurado no servidor")
    if x_admin_key != secret:
        raise HTTPException(status_code=401, detail="X-Admin-Key inválido")


@router.post("/dumps/clear-all")
async def clear_all_dumps(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_admin),
) -> dict:
    """Deleta TODOS os DumpItems do banco. Requer header X-Admin-Key."""
    result = await db.execute(delete(DumpItem))
    await db.commit()
    deleted = result.rowcount
    logger.warning("admin: {} dumps deletados via /admin/dumps/clear-all", deleted)
    return {"deleted_count": deleted}
