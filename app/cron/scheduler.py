from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from app.cron import morning_briefing, midday_checkin, nightly_closing, jira_sync, weekly_report, system_review

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
    # Plano B — 14:00 seg-sex (se nenhuma tarefa concluída)
    scheduler.add_job(
        midday_checkin.run_plan_b,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=0),
        id="plan_b",
        name="Plano B 14:00",
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
    # Ritual nudge — 09:30 seg-sex (se Pedro não respondeu ao briefing)
    scheduler.add_job(
        morning_briefing.run_ritual_nudge,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30),
        id="ritual_nudge",
        name="Ritual nudge 09:30",
        misfire_grace_time=300,
    )
    # Ritual nudge 1h — 10:00 seg-sex (mensagem final, depois para de insistir)
    scheduler.add_job(
        morning_briefing.run_ritual_nudge_1h,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=0),
        id="ritual_nudge_1h",
        name="Ritual nudge 10:00 (final)",
        misfire_grace_time=300,
    )
    # Revisão diária do sistema — 21:30 seg-sex (primeiros 30 dias)
    scheduler.add_job(
        system_review.run,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=30),
        id="system_review",
        name="System review 21:30",
        misfire_grace_time=300,
    )
    # Relatório semanal — domingo 10:00
    scheduler.add_job(
        weekly_report.run,
        CronTrigger(day_of_week="sun", hour=10, minute=0),
        id="weekly_report",
        name="Weekly report domingo 10:00",
        misfire_grace_time=300,
    )
    # Consolidação semanal de memórias — domingo 20:00
    scheduler.add_job(
        _run_weekly_consolidation,
        CronTrigger(day_of_week="sun", hour=20, minute=0),
        id="weekly_consolidation",
        name="Weekly memory consolidation domingo 20:00",
        misfire_grace_time=300,
    )
    # Export diário para Obsidian — 23:00 todo dia
    scheduler.add_job(
        _run_memory_export,
        CronTrigger(hour=23, minute=0),
        id="memory_export",
        name="Memory export Obsidian 23:00",
        misfire_grace_time=300,
    )
    logger.info("Cron jobs registered: {}", [j.id for j in scheduler.get_jobs()])


async def _run_weekly_consolidation() -> None:
    """Wrapper para consolidação semanal/mensal com acesso ao DB."""
    import datetime
    try:
        from app.database import AsyncSessionLocal
        from app.services import memory_manager
        async with AsyncSessionLocal() as db:
            await memory_manager.consolidate_weekly(db)
            # Se for o primeiro dia do mês, consolidar mensalmente também
            if datetime.date.today().day == 1:
                await memory_manager.consolidate_monthly(db)
    except Exception as exc:
        logger.error("weekly_consolidation failed: {}", exc)


async def _run_memory_export() -> None:
    """Wrapper para export Obsidian."""
    try:
        from app.services import memory_manager
        await memory_manager.export_to_obsidian()
    except Exception as exc:
        logger.error("memory_export failed: {}", exc)
