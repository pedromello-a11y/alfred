"""
nightly_closing.py — 21:00 seg-sex
1. Conta tarefas concluídas no dia
2. Calcula pontuação ponderada por esforço (calculate_points)
3. Atualiza streak
4. Gera memória diária via brain.consolidate_memory
5. Salva em memories + atualiza daily_plan como consolidated
6. Envia resumo
"""
from datetime import date, datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Memory, Streak, Task
from app.services import brain, task_manager, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            today = date.today()
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            # Tarefas concluídas hoje
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = result.scalars().all()

            # Tarefas ainda pendentes
            pending = await task_manager.get_pending(db)

            # Pontuação ponderada
            pontos = sum(task_manager.calculate_points(t) for t in done_today)

            # Atualizar/criar streak
            streak_count = await _update_streak(db, today, len(done_today), pontos)

            # Consolidar memória diária
            raw_data = _build_memory_data(done_today, pending, pontos)
            memory_text = await brain.consolidate_memory("daily", raw_data, db=db)

            memory = Memory(
                memory_type="daily",
                content=memory_text,
                period_start=today,
                period_end=today,
            )
            db.add(memory)

            # Marcar daily_plan como consolidated
            plan_result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan = plan_result.scalar_one_or_none()
            if plan:
                plan.consolidated = True
                plan.tasks_completed = {"ids": [str(t.id) for t in done_today]}
                plan.score = pontos

            await db.commit()

            # Gerar e enviar fechamento
            closing_context = _build_closing_context(done_today, pending, pontos, streak_count)
            closing_text = await brain.generate_closing(closing_context, db=db)
            await whapi_client.send_message(settings.pedro_phone, closing_text)
            logger.info("Nightly closing sent. Done={}, points={}, streak={}.", len(done_today), pontos, streak_count)

    except Exception as exc:
        logger.error("nightly_closing.run failed: {}", exc)


async def _update_streak(db, today: date, n_done: int, pontos: int) -> int:
    result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    last = result.scalar_one_or_none()

    from datetime import timedelta
    yesterday = today - timedelta(days=1)
    if last and last.streak_date == yesterday and n_done > 0:
        new_count = last.streak_count + 1
    elif n_done > 0:
        new_count = 1
    else:
        new_count = 0

    streak = Streak(
        streak_date=today,
        tasks_completed=n_done,
        points=pontos,
        streak_count=new_count,
    )
    db.add(streak)
    await db.flush()
    return new_count


def _build_memory_data(done: list, pending: list, pontos: int) -> str:
    lines = [f"Tarefas concluídas ({len(done)}):"]
    for t in done:
        lines.append(f"- {t.title}")
    lines.append(f"\nTarefas pendentes ({len(pending)}):")
    for t in pending[:5]:
        lines.append(f"- {t.title}")
    lines.append(f"\nPontos do dia: {pontos}")
    return "\n".join(lines)


def _build_closing_context(done: list, pending: list, pontos: int, streak: int) -> str:
    return (
        f"Gere o fechamento do dia para Pedro.\n"
        f"Concluídas hoje: {len(done)} tarefas\n"
        f"Ficaram pendentes: {len(pending)} tarefas\n"
        f"Pontos: {pontos}\n"
        f"Streak: {streak} dias\n"
        f"Formato WhatsApp, sem markdown."
    )
