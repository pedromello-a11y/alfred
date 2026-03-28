from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.cron import morning_briefing, midday_checkin, nightly_closing, jira_sync

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def setup_jobs() -> None:
    # Briefing preview — 07:00 seg-sex
    scheduler.add_job(
        morning_briefing.run_preview,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        id="morning_preview",
        name="Morning preview 07:00",
        misfire_grace_time=300,
    )
    # Briefing definitivo — 09:00 seg-sex
    scheduler.add_job(
        morning_briefing.run_full,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
        id="morning_briefing",
        name="Morning briefing 09:00",
        misfire_grace_time=300,
    )
    # Check-in contextual — 13:00 seg-sex
    scheduler.add_job(
        midday_checkin.run,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=0),
        id="midday_checkin",
        name="Midday check-in 13:00",
        misfire_grace_time=300,
    )
    # Fechamento noturno — 21:00 seg-sex
    scheduler.add_job(
        nightly_closing.run,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=0),
        id="nightly_closing",
        name="Nightly closing 21:00",
        misfire_grace_time=300,
    )
    # Jira sync — a cada 2h seg-sex (09:00, 11:00, 13:00, 15:00, 17:00)
    scheduler.add_job(
        jira_sync.run,
        CronTrigger(day_of_week="mon-fri", hour="9,11,13,15,17", minute=0),
        id="jira_sync",
        name="Jira sync 2h",
        misfire_grace_time=300,
    )
    logger.info("Cron jobs registered: {}", [j.id for j in scheduler.get_jobs()])
