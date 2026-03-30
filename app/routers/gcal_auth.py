"""
Rota temporária de autenticação Google Calendar.
Acesse /gcal/auth no browser para iniciar o OAuth flow.
Após autorizar, o refresh token é salvo automaticamente nas Settings do banco.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

from app.config import settings as app_settings
from app.database import get_db
from app.models import Settings

router = APIRouter(prefix="/gcal")

REDIRECT_URI = "https://web-production-62584.up.railway.app/gcal/callback"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


@router.get("/auth")
async def gcal_auth():
    """Inicia o OAuth flow — redireciona para o Google."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": app_settings.google_client_id,
                "client_secret": app_settings.google_client_secret,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return RedirectResponse(auth_url)


@router.get("/callback")
async def gcal_callback(code: str, db: AsyncSession = Depends(get_db)):
    """Recebe o code do Google e troca pelo refresh token."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": app_settings.google_client_id,
                "client_secret": app_settings.google_client_secret,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    flow.fetch_token(code=code)
    creds = flow.credentials
    refresh_token = creds.refresh_token

    if not refresh_token:
        return HTMLResponse("<h2>Erro: refresh_token não retornado. Tente revogar o acesso no Google e tentar novamente.</h2>")

    # Salva no banco como Settings
    for key, value in [
        ("google_refresh_token", refresh_token),
        ("google_client_id", app_settings.google_client_id),
        ("google_client_secret", app_settings.google_client_secret),
    ]:
        existing = await db.execute(select(Settings).where(Settings.key == key))
        setting = existing.scalar_one_or_none()
        if setting:
            setting.value = value
        else:
            db.add(Settings(key=key, value=value))

    await db.commit()
    logger.info("Google Calendar refresh token saved to database.")

    return HTMLResponse("""
    <html><body style="font-family:sans-serif;padding:40px;background:#111;color:#eee">
    <h2 style="color:#a855f7">✓ Google Calendar conectado!</h2>
    <p>Refresh token salvo com sucesso.</p>
    <p>Agora adicione as variáveis abaixo no Railway <strong>web → Variables</strong>:</p>
    <pre style="background:#222;padding:16px;border-radius:8px">
GOOGLE_REFRESH_TOKEN = """ + refresh_token + """
    </pre>
    <p>Depois faça redeploy do serviço web no Railway.</p>
    </body></html>
    """)
