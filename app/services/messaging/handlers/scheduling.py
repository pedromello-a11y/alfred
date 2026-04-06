"""
Handlers para intenção de agendar blocos no GCal e confirmação de eventos.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import gcal_client, task_manager


async def handle_schedule_intent(raw_text: str, db: AsyncSession) -> str:
    duration_min = 60
    dur_match = re.search(r"(\d+)\s*(h(?:ora)?s?|min(?:utos?)?)", raw_text, re.IGNORECASE)
    if dur_match:
        val = int(dur_match.group(1))
        unit = dur_match.group(2).lower()
        duration_min = val * 60 if unit.startswith("h") else val

    task_name_match = re.sub(
        r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)\s*",
        "", raw_text,
    ).strip()
    task_name = task_name_match[:100] if task_name_match else "Foco"

    events = await gcal_client.get_today_events()
    now = datetime.now(timezone.utc)
    start_candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_work = now.replace(hour=21, minute=0, second=0, microsecond=0)

    occupied = []
    for e in events:
        try:
            from dateutil.parser import parse as _parse
            es = _parse(e["start"]).astimezone(timezone.utc)
            ee = _parse(e["end"]).astimezone(timezone.utc)
            occupied.append((es, ee))
        except Exception:
            pass

    slot_start = start_candidate
    slot_found = False
    for _ in range(8):
        slot_end = slot_start + timedelta(minutes=duration_min)
        conflict = any(not (slot_end <= os or slot_start >= oe) for os, oe in occupied)
        if not conflict and slot_end <= end_work:
            slot_found = True
            break
        slot_start += timedelta(hours=1)

    if not slot_found:
        return "Não achei slot livre hoje. Quer agendar pra amanhã de manhã?"

    slot_brt_start = slot_start - timedelta(hours=3)
    slot_brt_end = (slot_start + timedelta(minutes=duration_min)) - timedelta(hours=3)
    time_str = f"{slot_brt_start.strftime('%H:%M')} → {slot_brt_end.strftime('%H:%M')}"
    proposal = {
        "title": task_name,
        "start": slot_start.isoformat(),
        "end": (slot_start + timedelta(minutes=duration_min)).isoformat(),
    }
    await task_manager.set_setting("pending_gcal_event", json.dumps(proposal), db)
    return f"Sugiro *{time_str}* pra '{task_name}' ({duration_min}min).\nCrio na agenda? Responde *sim* pra confirmar."


async def handle_gcal_confirm(db: AsyncSession) -> str:
    raw = await task_manager.get_setting("pending_gcal_event", db=db)
    if not raw:
        return "Não encontrei nenhum evento pendente de confirmação."
    proposal = json.loads(raw)
    await task_manager.set_setting("pending_gcal_event", "", db)
    start_dt = datetime.fromisoformat(proposal["start"])
    end_dt = datetime.fromisoformat(proposal["end"])
    result = await gcal_client.create_event(proposal["title"], start_dt, end_dt)
    if result.get("status") == "ok":
        return f"✅ Evento *{proposal['title']}* criado na agenda!"
    return f"Não consegui criar o evento: {result.get('error', 'erro desconhecido')}"
