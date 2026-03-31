"""
gcal_client.py — Google Calendar API (OAuth2 com refresh token)
Funções: get_today_events(), get_available_hours(), create_event()
Requer scopes: calendar.readonly + calendar.events (escrita)
"""
from datetime import datetime, timezone

from loguru import logger

from app.config import settings


async def _load_google_creds() -> dict:
    """
    Carrega credenciais Google: DB primeiro (salvas via /gcal/callback),
    fallback para variáveis de ambiente.
    """
    try:
        from sqlalchemy import select as sa_select
        from app.database import AsyncSessionLocal
        from app.models import Settings

        async with AsyncSessionLocal() as db:
            keys = ["google_refresh_token", "google_client_id", "google_client_secret"]
            result = await db.execute(sa_select(Settings).where(Settings.key.in_(keys)))
            rows = result.scalars().all()
            db_vals = {r.key: r.value for r in rows if r.value}

        return {
            "refresh_token": db_vals.get("google_refresh_token") or settings.google_refresh_token,
            "client_id": db_vals.get("google_client_id") or settings.google_client_id,
            "client_secret": db_vals.get("google_client_secret") or settings.google_client_secret,
        }
    except Exception as exc:
        logger.warning("Could not load Google creds from DB, using env: {}", exc)
        return {
            "refresh_token": settings.google_refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        }


def _build_service(refresh_token: str, client_id: str, client_secret: str):
    """Cria o serviço Google Calendar autenticado via refresh token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("calendar", "v3", credentials=creds)


def _calc_duration(event: dict) -> int:
    """Calcula duração do evento em minutos."""
    start = event["start"].get("dateTime")
    end = event["end"].get("dateTime")
    if not start or not end:
        return 0
    try:
        from dateutil.parser import parse
        return int((parse(end) - parse(start)).total_seconds() / 60)
    except Exception:
        return 0


async def get_today_events() -> list[dict]:
    """Retorna compromissos do dia no Google Calendar (primary)."""
    try:
        import asyncio

        creds = await _load_google_creds()
        if not creds["refresh_token"]:
            logger.warning("GCal: no refresh token configured.")
            return []

        service = await asyncio.get_event_loop().run_in_executor(
            None, _build_service, creds["refresh_token"], creds["client_id"], creds["client_secret"]
        )

        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        end = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

        def _fetch():
            return service.events().list(
                calendarId="primary",
                timeMin=start,
                timeMax=end,
                singleEvents=True,
                orderBy="startTime",
            ).execute()

        result = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        events = result.get("items", [])
        parsed = [
            {
                "title": e.get("summary", "Sem título"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
                "duration_minutes": _calc_duration(e),
            }
            for e in events
        ]
        logger.info("GCal: {} events today.", len(parsed))
        return parsed
    except Exception as exc:
        logger.error("get_today_events failed: {}", exc)
        return []


async def get_available_hours() -> float:
    """Horas disponíveis = 8h de trabalho - soma das reuniões do dia."""
    events = await get_today_events()
    meeting_minutes = sum(e["duration_minutes"] for e in events)
    available = max(0.0, 8 * 60 - meeting_minutes)
    return round(available / 60, 1)


async def create_event(
    title: str,
    start_dt: datetime,
    end_dt: datetime,
    description: str | None = None,
) -> dict:
    """
    Cria evento no Google Calendar (primary).
    Requer scope: https://www.googleapis.com/auth/calendar.events
    Retorna {'status': 'ok', 'event_id': ..., 'html_link': ...} ou {'status': 'failed', 'error': ...}
    """
    try:
        import asyncio

        creds = await _load_google_creds()
        if not creds["refresh_token"]:
            return {"status": "failed", "error": "no refresh token configured"}

        def _create():
            service = _build_service(creds["refresh_token"], creds["client_id"], creds["client_secret"])
            body = {
                "summary": title,
                "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
                "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
            }
            if description:
                body["description"] = description
            return service.events().insert(calendarId="primary", body=body).execute()

        event = await asyncio.get_event_loop().run_in_executor(None, _create)
        logger.info("GCal event created: {} ({} → {})", title, start_dt, end_dt)
        return {"status": "ok", "event_id": event.get("id"), "html_link": event.get("htmlLink")}
    except Exception as exc:
        logger.error("create_event failed: {}", exc)
        return {"status": "failed", "error": str(exc)}
