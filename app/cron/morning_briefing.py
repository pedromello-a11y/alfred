"""
morning_briefing.py
  - run_preview(): 07:00 — aviso leve do dia
  - run_full():    09:00 — briefing definitivo com plano completo
"""
from datetime import date, datetime

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Streak, Task
from app.services import brain, task_manager, whapi_client


async def run_preview() -> None:
    """07:00 — preview leve: quantas reuniões e tarefas prioritárias."""
    try:
        async with AsyncSessionLocal() as db:
            tasks = await task_manager.get_pending(db)
            top = tasks[:3]
            n_tasks = len(tasks)
            hoje = date.today().strftime("%d/%m")
            lines = [f"Bom dia! ☀️ Hoje ({hoje}) você tem *{n_tasks} tarefas* pendentes."]
            if top:
                lines.append("Top 3 prioridades:")
                for i, t in enumerate(top, 1):
                    lines.append(f"{i}. {t.title}")
            lines.append("Briefing completo às 9h.")
            await whapi_client.send_message(settings.pedro_phone, "\n".join(lines))
            logger.info("Morning preview sent.")
    except Exception as exc:
        logger.error("morning_briefing.run_preview failed: {}", exc)


async def run_full() -> None:
    """09:00 — briefing definitivo: plano do dia salvo em daily_plans."""
    try:
        async with AsyncSessionLocal() as db:
            tasks = await task_manager.get_pending(db)
            streak = await _get_streak(db)

            # Calcular scores e ordenar
            scored = sorted(tasks, key=lambda t: task_manager.calculate_priority_score(t, current_streak=streak), reverse=True)
            top = scored[:3]

            # Marcar tarefas como planejadas (backlog decay)
            today = date.today()
            for t in scored:
                t.times_planned = (t.times_planned or 0) + 1
                t.last_planned = today
            await db.commit()

            context = _build_briefing_context(top, streak, today)
            briefing_text = await brain.generate_briefing(context, db=db)

            # Salvar daily_plan
            plan = DailyPlan(
                plan_date=today,
                plan_content=briefing_text,
                tasks_planned={"ids": [str(t.id) for t in top]},
            )
            db.add(plan)
            await db.commit()

            await whapi_client.send_message(settings.pedro_phone, briefing_text)
            logger.info("Morning briefing sent for {}.", today)
    except Exception as exc:
        logger.error("morning_briefing.run_full failed: {}", exc)


def _build_briefing_context(top_tasks: list, streak: int, today: date) -> str:
    dia = today.strftime("%A %d/%m")
    lines = [
        f"Gere o briefing do dia para Pedro. Hoje é {dia}. Streak: {streak} dias.",
        "Tarefas prioritárias de hoje:",
    ]
    for i, t in enumerate(top_tasks, 1):
        prazo = t.deadline.strftime("%d/%m") if t.deadline else "sem prazo"
        lines.append(f"{i}. {t.title} (prazo: {prazo})")
    lines.append("Formato WhatsApp, sem markdown, máx 3-4 linhas por bloco.")
    return "\n".join(lines)


async def _get_streak(db) -> int:
    result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak = result.scalar_one_or_none()
    return streak.streak_count if streak else 0
