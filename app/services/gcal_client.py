"""
gcal_client.py — Google Calendar API (OAuth2 com refresh token)
Funções: get_today_events(), get_available_hours()
"""
from datetime import datetime, timezone

from loguru import logger

from app.config import settings


def _build_service():
    """Cria o serviço Google Calendar autenticado via refresh token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
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
        service = await asyncio.get_event_loop().run_in_executor(None, _build_service)

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
