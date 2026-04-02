"""Consolidação hierárquica de memória do Alfred.

- Diária (22h seg-sex): sintetiza o dia em ~200 palavras
- Semanal (dom 20h): consolida memórias diárias da semana em ~300 palavras
- Mensal (dia 1, 19h): mantém apenas insights de longo prazo em ~200 palavras
- Recovery: se consolidação diária falhou, recupera antes da próxima semanal
"""
from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import DailyPlan, Memory, Streak, Task
from app.services import brain
from app.services.time_utils import today_brt


async def run_daily() -> None:
    """Sintetiza memória do dia: tasks feitas, score, eventos relevantes."""
    try:
        async with AsyncSessionLocal() as db:
            today = today_brt()

            existing = await db.execute(
                select(Memory)
                .where(Memory.memory_type == "daily")
                .where(Memory.period_start == today)
                .where(Memory.superseded == False)
            )
            if existing.scalar_one_or_none():
                logger.debug("Daily memory already exists for {}", today)
                return

            start = datetime.combine(today, datetime.min.time())
            done_result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= start)
            )
            done_today = list(done_result.scalars().all())

            plan_result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan = plan_result.scalar_one_or_none()

            streak_result = await db.execute(
                select(Streak).where(Streak.streak_date == today)
            )
            streak = streak_result.scalar_one_or_none()

            raw_data = f"Data: {today.strftime('%d/%m/%Y')}\n"
            raw_data += f"Tasks concluídas: {len(done_today)}\n"
            for t in done_today:
                raw_data += f"- {t.title} ({t.category or 'work'})\n"
                if t.notes:
                    raw_data += f"  Notas: {t.notes[:200]}\n"
            if plan:
                raw_data += f"Score do dia: {plan.score or 0}\n"
            if streak:
                raw_data += f"Streak: {streak.streak_count} dias\n"
                raw_data += f"Pontos: {streak.points}\n"

            content = await brain.consolidate_memory("daily", raw_data, db=db)
            if not content:
                content = raw_data

            memory = Memory(
                memory_type="daily",
                content=content,
                period_start=today,
                period_end=today,
            )
            db.add(memory)
            await db.commit()
            logger.info("Daily memory consolidated for {}", today)
    except Exception as exc:
        logger.error("memory_consolidation.run_daily failed: {}", exc)


async def run_weekly() -> None:
    """Consolida memórias diárias da semana. Recupera dias faltantes primeiro."""
    try:
        async with AsyncSessionLocal() as db:
            today = today_brt()
            week_start = today - timedelta(days=today.weekday())
            week_end = today

            # Recovery: check for missing daily consolidations this week
            for day_offset in range(7):
                check_date = week_start + timedelta(days=day_offset)
                if check_date > today:
                    break
                if check_date.weekday() >= 5:
                    continue
                existing = await db.execute(
                    select(Memory)
                    .where(Memory.memory_type == "daily")
                    .where(Memory.period_start == check_date)
                    .where(Memory.superseded == False)
                )
                if not existing.scalar_one_or_none():
                    logger.warning("Missing daily memory for {}, attempting recovery", check_date)
                    await _recover_daily(db, check_date)

            existing_weekly = await db.execute(
                select(Memory)
                .where(Memory.memory_type == "weekly")
                .where(Memory.period_start == week_start)
                .where(Memory.superseded == False)
            )
            if existing_weekly.scalar_one_or_none():
                logger.debug("Weekly memory already exists for week starting {}", week_start)
                return

            daily_result = await db.execute(
                select(Memory)
                .where(Memory.memory_type == "daily")
                .where(Memory.period_start >= week_start)
                .where(Memory.period_end <= week_end)
                .where(Memory.superseded == False)
                .order_by(Memory.period_start.asc())
            )
            dailies = list(daily_result.scalars().all())

            raw_data = f"Semana: {week_start.strftime('%d/%m')} a {week_end.strftime('%d/%m/%Y')}\n\n"
            for mem in dailies:
                raw_data += f"--- {mem.period_start.strftime('%A %d/%m')} ---\n"
                raw_data += mem.content + "\n\n"

            content = await brain.consolidate_memory("weekly", raw_data, db=db)
            if not content:
                content = raw_data

            for mem in dailies:
                mem.superseded = True

            memory = Memory(
                memory_type="weekly",
                content=content,
                period_start=week_start,
                period_end=week_end,
            )
            db.add(memory)
            await db.commit()
            logger.info("Weekly memory consolidated for week starting {}", week_start)
    except Exception as exc:
        logger.error("memory_consolidation.run_weekly failed: {}", exc)


async def run_monthly() -> None:
    """Consolida memórias semanais do mês anterior."""
    try:
        async with AsyncSessionLocal() as db:
            today = today_brt()
            month_start = today.replace(day=1)
            prev_month_end = month_start - timedelta(days=1)
            prev_month_start = prev_month_end.replace(day=1)

            existing = await db.execute(
                select(Memory)
                .where(Memory.memory_type == "monthly")
                .where(Memory.period_start == prev_month_start)
                .where(Memory.superseded == False)
            )
            if existing.scalar_one_or_none():
                return

            weekly_result = await db.execute(
                select(Memory)
                .where(Memory.memory_type == "weekly")
                .where(Memory.period_start >= prev_month_start)
                .where(Memory.period_end <= prev_month_end)
                .where(Memory.superseded == False)
                .order_by(Memory.period_start.asc())
            )
            weeklies = list(weekly_result.scalars().all())

            if not weeklies:
                return

            raw_data = f"Mês: {prev_month_start.strftime('%B %Y')}\n\n"
            for mem in weeklies:
                raw_data += f"--- Semana {mem.period_start.strftime('%d/%m')} ---\n"
                raw_data += mem.content + "\n\n"

            content = await brain.consolidate_memory("monthly", raw_data, db=db)
            if not content:
                content = raw_data

            for mem in weeklies:
                mem.superseded = True

            memory = Memory(
                memory_type="monthly",
                content=content,
                period_start=prev_month_start,
                period_end=prev_month_end,
            )
            db.add(memory)
            await db.commit()
            logger.info("Monthly memory consolidated for {}", prev_month_start.strftime("%B %Y"))
    except Exception as exc:
        logger.error("memory_consolidation.run_monthly failed: {}", exc)


async def _recover_daily(db, target_date: date) -> None:
    """Attempts to recover a failed daily consolidation."""
    start = datetime.combine(target_date, datetime.min.time())
    end = datetime.combine(target_date + timedelta(days=1), datetime.min.time())

    done_result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .where(Task.completed_at >= start)
        .where(Task.completed_at < end)
    )
    done = list(done_result.scalars().all())

    raw_data = f"Data (recuperação): {target_date.strftime('%d/%m/%Y')}\n"
    raw_data += f"Tasks concluídas: {len(done)}\n"
    for t in done:
        raw_data += f"- {t.title}\n"

    content = await brain.consolidate_memory("daily", raw_data, db=db)
    if not content:
        content = raw_data

    memory = Memory(
        memory_type="daily",
        content=content,
        period_start=target_date,
        period_end=target_date,
    )
    db.add(memory)
    await db.flush()
    logger.info("Recovered daily memory for {}", target_date)
