from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.cron import end_of_day_stack, morning_briefing_rebuild, midday_checkin_rebuild

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def setup_jobs() -> None:
    scheduler.add_job(morning_briefing_rebuild.run_preview, CronTrigger(day_of_week="mon-fri", hour=7, minute=0), id="final_preview")
    scheduler.add_job(morning_briefing_rebuild.run_full, CronTrigger(day_of_week="mon-fri", hour=9, minute=0), id="final_briefing")
    scheduler.add_job(morning_briefing_rebuild.run_ritual_nudge, CronTrigger(day_of_week="mon-fri", hour=9, minute=30), id="final_nudge")
    scheduler.add_job(morning_briefing_rebuild.run_ritual_nudge_1h, CronTrigger(day_of_week="mon-fri", hour=10, minute=0), id="final_nudge_1h")
    scheduler.add_job(midday_checkin_rebuild.run, CronTrigger(day_of_week="mon-fri", hour=13, minute=0), id="final_midday")
    scheduler.add_job(midday_checkin_rebuild.run_plan_b, CronTrigger(day_of_week="mon-fri", hour=14, minute=0), id="final_plan_b")
    scheduler.add_job(end_of_day_stack.run, CronTrigger(day_of_week="mon-fri", hour=21, minute=0), id="final_end")
