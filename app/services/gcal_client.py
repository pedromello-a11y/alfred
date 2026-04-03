"""Google Calendar client — real implementation using OAuth2 + httpx."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger

from app.config import settings
from app.services.time_utils import today_brt, to_brt_naive

_GCAL_API = "https://www.googleapis.com/calendar/v3"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _is_configured() -> bool:
    return bool(
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_refresh_token
    )


async def _get_access_token() -> str | None:
    """Refreshes and returns a short-lived access token."""
    if not _is_configured():
        return None
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "refresh_token": settings.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code != 200:
            logger.warning("gcal token refresh failed: {} {}", resp.status_code, resp.text[:200])
            return None
        return resp.json().get("access_token")


def _parse_event(event: dict) -> dict:
    """Normalizes a GCal event dict to {title, start, end, id}."""
    start_raw = event.get("start", {})
    end_raw = event.get("end", {})

    def _parse_dt(raw: dict) -> datetime | None:
        if "dateTime" in raw:
            try:
                return datetime.fromisoformat(raw["dateTime"])
            except ValueError:
                return None
        if "date" in raw:
            try:
                dt = datetime.fromisoformat(raw["date"])
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None

    return {
        "id": event.get("id", ""),
        "title": event.get("summary", "(sem título)"),
        "start": _parse_dt(start_raw),
        "end": _parse_dt(end_raw),
        "description": event.get("description", ""),
    }


async def get_today_events() -> list[dict]:
    """Returns today's events from the primary calendar (BRT)."""
    from app.services.time_utils import BRT
    today = today_brt()
    day_start = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=BRT)
    day_end = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=BRT)
    return await get_events_range(day_start, day_end)


async def get_events_range(start_dt: datetime, end_dt: datetime) -> list[dict]:
    """Returns events between start_dt and end_dt."""
    if not _is_configured():
        return []
    token = await _get_access_token()
    if not token:
        return []
    params = {
        "calendarId": "primary",
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 50,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_GCAL_API}/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status_code != 200:
            logger.warning("gcal get_events failed: {} {}", resp.status_code, resp.text[:200])
            return []
        items = resp.json().get("items", [])
        return [_parse_event(e) for e in items]


async def create_event(
    title: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
) -> dict:
    """Creates an event. Returns {status, event_id} or {status, error}."""
    if not _is_configured():
        return {"status": "error", "error": "gcal not configured"}
    token = await _get_access_token()
    if not token:
        return {"status": "error", "error": "token refresh failed"}

    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if description:
        body["description"] = description

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_GCAL_API}/calendars/primary/events",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )
        if resp.status_code not in (200, 201):
            logger.warning("gcal create_event failed: {} {}", resp.status_code, resp.text[:200])
            return {"status": "error", "error": f"api error {resp.status_code}"}
        event_id = resp.json().get("id", "")
        logger.info("gcal event created: {} (id={})", title, event_id)
        return {"status": "ok", "event_id": event_id}


async def delete_event(event_id: str) -> dict:
    """Deletes an event by ID."""
    if not _is_configured():
        return {"status": "error", "error": "gcal not configured"}
    token = await _get_access_token()
    if not token:
        return {"status": "error", "error": "token refresh failed"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{_GCAL_API}/calendars/primary/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code not in (200, 204):
            return {"status": "error", "error": f"api error {resp.status_code}"}
        return {"status": "ok"}


async def sync_to_agenda_blocks(db) -> int:
    """Syncs current week's GCal events (Mon-Fri, BRT) into AgendaBlock table. Returns count."""
    from app.services.agenda_manager import upsert_agenda_block
    from app.services.time_utils import BRT

    today = today_brt()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    week_start = datetime(monday.year, monday.month, monday.day, 0, 0, 0, tzinfo=BRT)
    week_end = datetime(friday.year, friday.month, friday.day, 23, 59, 59, tzinfo=BRT)

    events = await get_events_range(week_start, week_end)
    if not events:
        logger.info("gcal sync: no events returned for week {}-{}", monday, friday)
        return 0

    synced = 0
    for event in events:
        if not event.get("start") or not event.get("end"):
            continue
        block_type = _infer_block_type(event)
        try:
            await upsert_agenda_block(
                title=event["title"],
                start_at=to_brt_naive(event["start"]),
                end_at=to_brt_naive(event["end"]),
                block_type=block_type,
                source="gcal",
                db=db,
            )
            synced += 1
        except Exception as exc:
            logger.warning("gcal sync failed for event {}: {}", event.get("id"), exc)
    logger.info("gcal sync: {} events synced for week {}-{}", synced, monday, friday)
    return synced


def _infer_block_type(event: dict) -> str:
    title = (event.get("title") or "").lower()
    if any(w in title for w in ("meeting", "reunião", "reuniao", "call", "sync", "review")):
        return "meeting"
    if any(w in title for w in ("break", "almoço", "almoco", "pausa", "descanso")):
        return "break"
    if any(w in title for w in ("admin", "email", "emails", "inbox")):
        return "admin"
    if any(w in title for w in ("pessoal", "personal", "saúde", "saude", "médico", "medico")):
        return "personal"
    return "focus"
