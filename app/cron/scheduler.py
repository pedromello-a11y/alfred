"""Scheduler único do Alfred — todos os jobs cron registrados aqui."""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.cron import end_of_day_stack, midday_checkin, morning_briefing

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def setup_jobs() -> None:
    """Registra todos os jobs cron do Alfred."""
    scheduler.add_job(morning_briefing.run_preview, CronTrigger(day_of_week="mon-fri", hour=7, minute=0), id="preview")
    scheduler.add_job(morning_briefing.run_full, CronTrigger(day_of_week="mon-fri", hour=9, minute=0), id="briefing")
    scheduler.add_job(morning_briefing.run_ritual_nudge, CronTrigger(day_of_week="mon-fri", hour=9, minute=30), id="nudge")
    scheduler.add_job(morning_briefing.run_ritual_nudge_1h, CronTrigger(day_of_week="mon-fri", hour=10, minute=0), id="nudge_1h")
    scheduler.add_job(midday_checkin.run, CronTrigger(day_of_week="mon-fri", hour=13, minute=0), id="midday")
    scheduler.add_job(midday_checkin.run_plan_b, CronTrigger(day_of_week="mon-fri", hour=14, minute=0), id="plan_b")
    scheduler.add_job(end_of_day_stack.run, CronTrigger(day_of_week="mon-fri", hour=21, minute=0), id="end_of_day")
