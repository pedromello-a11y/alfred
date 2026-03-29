from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.cron.scheduler import scheduler, setup_jobs
from app.database import init_db
from app.routers import health, webhook


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Alfred starting up...")
    await init_db()
    logger.info("Database initialized.")
    setup_jobs()
    scheduler.start()
    logger.info("Scheduler started.")
    await _check_missed_jobs()
    yield
    scheduler.shutdown(wait=False)
    logger.info("Alfred shutting down.")


async def _check_missed_jobs() -> None:
    """
    C4 — Startup resilience: se o servidor reiniciou durante o horário de trabalho
    e algum job crítico não rodou hoje, dispara agora.
    """
    from datetime import date, datetime, timezone
    from app.database import AsyncSessionLocal
    from app.models import DailyPlan
    from sqlalchemy import select

    try:
        now = datetime.now(timezone.utc)
        # Converter para horário de Brasília (UTC-3)
        hour_brt = (now.hour - 3) % 24

        async with AsyncSessionLocal() as db:
            today = date.today()

            # Checar se briefing já rodou hoje (daily_plan existe)
            result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan_exists = result.scalar_one_or_none() is not None

            if not plan_exists and hour_brt >= 9:
                logger.warning("Startup: daily_plan missing and it's past 09:00 BRT — running missed briefing.")
                from app.cron import morning_briefing
                await morning_briefing.run_full()

            if hour_brt >= 21:
                # Checar se nightly já rodou (daily_plan consolidated)
                result2 = await db.execute(
                    select(DailyPlan).where(DailyPlan.plan_date == today)
                )
                plan = result2.scalar_one_or_none()
                if plan and not plan.consolidated:
                    logger.warning("Startup: nightly closing not yet run today — running now.")
                    from app.cron import nightly_closing
                    await nightly_closing.run()

    except Exception as exc:
        logger.error("_check_missed_jobs failed: {}", exc)


app = FastAPI(title="Alfred", lifespan=lifespan)

app.include_router(health.router)
app.include_router(webhook.router)
