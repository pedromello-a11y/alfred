"""Scheduler simples que roda checks periódicos de alertas."""
import asyncio
import logging
from datetime import timedelta
from datetime import time as dt_time

from app.database import AsyncSessionLocal
from app.services.time_utils import now_brt
from app.services.whatsapp_alerts import check_and_send_alerts, send_pre_meeting_alert, check_blocked_reminders

logger = logging.getLogger(__name__)

_scheduler_running = False


async def _get_send_fn():
    """Retorna função para enviar WhatsApp ao dono."""
    try:
        from app.services.whapi_client import send_message
        from app.config import settings

        async def send_fn(text: str) -> None:
            phone = settings.pedro_phone or settings.allowed_chat_id
            if phone:
                await send_message(phone, text)

        return send_fn
    except Exception:
        return None


async def run_periodic_checks() -> None:
    """Loop que roda a cada 30 minutos checando alertas."""
    global _scheduler_running
    if _scheduler_running:
        return
    _scheduler_running = True
    logger.info("Alert scheduler started")

    while True:
        try:
            agora = now_brt()
            hora = agora.time()

            if dt_time(9, 0) <= hora <= dt_time(20, 0):
                send_fn = await _get_send_fn()
                if send_fn:
                    async with AsyncSessionLocal() as db:
                        await check_and_send_alerts(db, send_fn)

                    # Check bloqueadas (só uma vez por dia, às 10h)
                    if dt_time(10, 0) <= hora <= dt_time(10, 30):
                        try:
                            async with AsyncSessionLocal() as db:
                                await check_blocked_reminders(db, send_fn)
                        except Exception:
                            pass

                    # Check reuniões nos próximos 6 minutos
                    try:
                        from app.services.gcal_client import get_events_range
                        events = await get_events_range(agora, agora + timedelta(minutes=6))
                        for ev in (events or []):
                            await send_pre_meeting_alert(ev, send_fn)
                    except Exception:
                        pass

        except Exception as exc:
            logger.error("Alert scheduler error: %s", exc)

        await asyncio.sleep(1800)


def start_scheduler() -> None:
    """Inicia o scheduler como background task."""
    asyncio.ensure_future(run_periodic_checks())
