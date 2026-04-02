"""Scheduler único do Alfred — todos os jobs cron registrados aqui."""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.cron import (
    backlog_cleanup,
    gcal_sync,
    jira_sync,
    memory_consolidation,
    midday_checkin,
    morning_briefing,
    nightly_closing,
    task_timer_nudge,
)

scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")


def setup_jobs() -> None:
    """Registra todos os jobs cron do Alfred."""
    # ── Briefing matinal ──────────────────────────────────────────────────
    scheduler.add_job(
        morning_briefing.run_preview,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        id="preview",
    )
    scheduler.add_job(
        morning_briefing.run_full,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0),
        id="briefing",
    )
    scheduler.add_job(
        morning_briefing.run_ritual_nudge,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30),
        id="nudge",
    )
    scheduler.add_job(
        morning_briefing.run_ritual_nudge_1h,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=0),
        id="nudge_1h",
    )

    # ── Check-ins do dia ──────────────────────────────────────────────────
    scheduler.add_job(
        midday_checkin.run,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=0),
        id="midday",
    )
    scheduler.add_job(
        midday_checkin.run_plan_b,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0),
        id="plan_b",
    )

    # ── Fechamento noturno ────────────────────────────────────────────────
    scheduler.add_job(
        nightly_closing.run,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=0),
        id="nightly_closing",
    )

    # ── Memória hierárquica ───────────────────────────────────────────────
    scheduler.add_job(
        memory_consolidation.run_daily,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=0),
        id="memory_daily",
    )
    scheduler.add_job(
        memory_consolidation.run_weekly,
        CronTrigger(day_of_week="sun", hour=20, minute=0),
        id="memory_weekly",
    )
    scheduler.add_job(
        memory_consolidation.run_monthly,
        CronTrigger(day=1, hour=19, minute=0),
        id="memory_monthly",
    )

    # ── Timer nudge (task ativa há 2h+) ──────────────────────────────────
    scheduler.add_job(
        task_timer_nudge.run,
        CronTrigger(day_of_week="mon-fri", hour="9-20", minute="15,45"),
        id="task_timer_nudge",
    )

    # ── Limpeza de backlog pessoal (segunda 8h30) ─────────────────────────
    scheduler.add_job(
        backlog_cleanup.run,
        CronTrigger(day_of_week="mon", hour=8, minute=30),
        id="backlog_cleanup",
    )

    # ── Google Calendar sync (seg-sex 7h-21h, a cada 30min) ──────────────
    scheduler.add_job(
        gcal_sync.run,
        CronTrigger(day_of_week="mon-fri", hour="7-21", minute="0,30"),
        id="gcal_sync",
    )

    # ── Jira sync (seg-sex 8h-20h, a cada hora) ──────────────────────────
    scheduler.add_job(
        jira_sync.run,
        CronTrigger(day_of_week="mon-fri", hour="8-20", minute=0),
        id="jira_sync",
    )
