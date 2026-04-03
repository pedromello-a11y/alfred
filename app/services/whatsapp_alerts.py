"""Envia alertas proativos via WhatsApp."""
from datetime import datetime, time
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services.time_utils import now_brt
from app.services.daily_briefing import generate_deadline_alert


async def check_and_send_alerts(db: AsyncSession, send_fn) -> None:
    """
    Chamado periodicamente (a cada 30min via scheduler).
    send_fn = função async que envia msg WhatsApp: async send_fn(text: str)
    """
    agora = now_brt()
    hoje = agora.date()
    hora_atual = agora.time()

    # Só alertar em horário comercial (9h-19h)
    if hora_atual < time(9, 0) or hora_atual > time(19, 0):
        return

    result = await db.execute(
        select(Task).where(
            and_(
                Task.status.in_(["pending", "in_progress"]),
                Task.deadline.isnot(None),
                Task.category != "personal",
            )
        )
    )
    tasks = result.scalars().all()

    for t in tasks:
        dl = t.deadline
        if not dl:
            continue
        dl_date = dl.date() if hasattr(dl, "date") else dl
        if dl_date != hoje:
            continue

        # Se deadline é hoje e faltam menos de 3h
        if hasattr(dl, "hour"):
            dl_dt = dl
        else:
            dl_dt = datetime.combine(dl_date, time(17, 0))

        agora_naive = agora.replace(tzinfo=None) if agora.tzinfo else agora
        dl_naive = dl_dt.replace(tzinfo=None) if dl_dt.tzinfo else dl_dt
        diff = dl_naive - agora_naive
        hours_left = diff.total_seconds() / 3600
        if 0 < hours_left <= 3:
            msg = await generate_deadline_alert(t)
            try:
                await send_fn(msg)
            except Exception:
                pass


async def send_pre_meeting_alert(event: dict, send_fn) -> None:
    """Envia alerta 5min antes de reunião."""
    title = event.get("summary", "Reunião")
    start = event.get("start", {}).get("dateTime", "")
    hora = ""
    if start:
        try:
            dt = datetime.fromisoformat(start)
            hora = dt.strftime("%H:%M")
        except Exception:
            pass
    msg = f"📅 *{title}* começa em 5 minutos ({hora})."
    try:
        await send_fn(msg)
    except Exception:
        pass
