"""OAuth2 do Google Calendar — autorização, callback, status e desconexão."""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services import oauth_store

router = APIRouter(prefix="/auth/google", tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPES = " ".join([
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
])


@router.get("", response_class=RedirectResponse)
async def google_auth_start():
    """Redireciona para o consent screen do Google."""
    if not settings.is_gcal_configured:
        return JSONResponse(
            {"error": "GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET não configurados"},
            status_code=500,
        )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/callback", response_class=HTMLResponse)
async def google_auth_callback(
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Recebe o code do Google, troca por tokens e salva no banco."""
    if error or not code:
        msg = error or "código de autorização ausente"
        logger.warning("google oauth callback error: {}", msg)
        return HTMLResponse(_html_error(msg), status_code=400)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        logger.error("google token exchange failed: {} {}", resp.status_code, resp.text[:300])
        return HTMLResponse(_html_error(f"Falha na troca de token: {resp.status_code}"), status_code=502)

    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        logger.error("google token exchange: no refresh_token in response")
        return HTMLResponse(
            _html_error("Google não retornou refresh_token. Revogue o acesso em myaccount.google.com e tente novamente."),
            status_code=400,
        )

    await oauth_store.save_google_token(refresh_token, db)
    logger.info("google oauth: refresh_token salvo no banco")
    return HTMLResponse(_html_success())


@router.get("/status")
async def google_auth_status(db: AsyncSession = Depends(get_db)):
    """Retorna se o Google Calendar está conectado."""
    row = await oauth_store.get_google_token(db)
    if row and row.is_valid:
        return {"connected": True, "source": "database"}
    if settings.google_refresh_token:
        return {"connected": True, "source": "env_var"}
    return {"connected": False}


@router.post("/disconnect")
async def google_auth_disconnect(db: AsyncSession = Depends(get_db)):
    """Invalida o token salvo no banco."""
    await oauth_store.invalidate_google_token(db)
    logger.info("google oauth: token invalidado pelo usuário")
    return {"status": "ok", "message": "Google Calendar desconectado"}


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _html_success() -> str:
    return """<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><title>Alfred — Google Calendar</title>
<style>body{font-family:sans-serif;text-align:center;padding:60px;background:#f0fdf4}
h1{color:#16a34a;font-size:2rem}p{color:#374151;font-size:1.1rem}</style></head>
<body>
<h1>✅ Google Calendar conectado com sucesso!</h1>
<p>O Alfred já pode acessar sua agenda.</p>
<p>Pode fechar esta aba.</p>
</body></html>"""


def _html_error(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><title>Alfred — Erro</title>
<style>body{{font-family:sans-serif;text-align:center;padding:60px;background:#fef2f2}}
h1{{color:#dc2626;font-size:2rem}}p{{color:#374151;font-size:1.1rem}}</style></head>
<body>
<h1>❌ Erro na autorização</h1>
<p>{message}</p>
</body></html>"""
