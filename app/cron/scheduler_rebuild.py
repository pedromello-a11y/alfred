from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.cron import morning_briefing_rebuild, midday_checkin_rebuild, nightly_closing_rebuild

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def setup_jobs() -> None:
    scheduler.add_job(
        morning_briefing_rebuild.run_preview,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        id="rebuild_morning_preview",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        morning_briefing_rebuild.run_full,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
        id="rebuild_morning_briefing",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        morning_briefing_rebuild.run_ritual_nudge,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30),
        id="rebuild_ritual_nudge",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        morning_briefing_rebuild.run_ritual_nudge_1h,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=0),
        id="rebuild_ritual_nudge_1h",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        midday_checkin_rebuild.run,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=0),
        id="rebuild_midday_checkin",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        midday_checkin_rebuild.run_plan_b,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0),
        id="rebuild_plan_b",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        nightly_closing_rebuild.run,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=0),
        id="rebuild_nightly_closing",
        misfire_grace_time=300,
    )
