import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

import app.services.whapi_client as legacy_whapi_client

if (os.getenv("WA_GATEWAY_URL") or "").strip():
    from app.services.wa_bridge_client import send_message as gateway_send_message

    legacy_whapi_client.send_message = gateway_send_message
    logger.info("WA gateway mode enabled: legacy outbound patched to wa_bridge_client.send_message")

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.routers import health, wa_bridge, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Alfred gateway app starting up...")
    await init_db()
    logger.info("Database initialized.")
    setup_jobs()
    scheduler.start()
    logger.info("Scheduler started.")
    await _check_missed_jobs()
    yield
    scheduler.shutdown(wait=False)
    logger.info("Alfred gateway app shutting down.")


async def _check_missed_jobs() -> None:
    from datetime import date, datetime, timezone

    from sqlalchemy import select

    from app.cron import morning_briefing, nightly_closing
    from app.database import AsyncSessionLocal
    from app.models import DailyPlan

    try:
        now = datetime.now(timezone.utc)
        hour_brt = (now.hour - 3) % 24

        async with AsyncSessionLocal() as db:
            today = date.today()

            result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan_exists = result.scalar_one_or_none() is not None

            if not plan_exists and hour_brt >= 9:
                logger.warning("Startup: daily_plan missing and it's past 09:00 BRT — running missed briefing.")
                await morning_briefing.run_full()

            if hour_brt >= 21:
                result2 = await db.execute(
                    select(DailyPlan).where(DailyPlan.plan_date == today)
                )
                plan = result2.scalar_one_or_none()
                if plan and not plan.consolidated:
                    logger.warning("Startup: nightly closing not yet run today — running now.")
                    await nightly_closing.run()

    except Exception as exc:
        logger.error("_check_missed_jobs failed: {}", exc)


app = FastAPI(title="Alfred Gateway Mode", lifespan=lifespan)

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(wa_bridge.router)
