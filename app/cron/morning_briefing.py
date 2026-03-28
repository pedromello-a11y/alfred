"""
morning_briefing.py
  - run_preview(): 07:00 — aviso leve do dia
  - run_full():    09:00 — briefing definitivo com plano completo, Jira, GCal e vitória do dia
"""
from datetime import date, datetime

from loguru import logger
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Streak, Task
from app.services import brain, gcal_client, jira_client, task_manager, whapi_client


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
            scored = sorted(
                tasks,
                key=lambda t: task_manager.calculate_priority_score(t, current_streak=streak),
                reverse=True,
            )
            top = scored[:3]

            # Marcar tarefas como planejadas (backlog decay) + boss fight detection
            today = date.today()
            for t in scored:
                t.times_planned = (t.times_planned or 0) + 1
                t.last_planned = today
                # Boss fight: >120 min estimados ou planejado 3+ vezes
                if not t.is_boss_fight:
                    if (t.estimated_minutes and t.estimated_minutes > 120) or t.times_planned >= 3:
                        t.is_boss_fight = True
                        logger.info("Boss fight detectado: {}", t.title)
            await db.commit()

            # Buscar dados Jira e GCal em paralelo
            import asyncio
            jira_issues, available_hours, events = await asyncio.gather(
                jira_client.get_cached_issues(db),
                gcal_client.get_available_hours(),
                gcal_client.get_today_events(),
            )

            # Vitória do dia: tarefa #1 por priority_score
            daily_victory = top[0] if top else None
            if daily_victory:
                await task_manager.set_setting(
                    "daily_victory_task_id", str(daily_victory.id), db
                )

            context = _build_briefing_context(
                top, streak, today, jira_issues, available_hours, events, daily_victory
            )
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


def _build_briefing_context(
    top_tasks: list,
    streak: int,
    today: date,
    jira_issues: list,
    available_hours: float,
    events: list,
    daily_victory,
) -> str:
    dia = today.strftime("%A %d/%m")
    lines = [
        f"Gere o briefing do dia para Pedro. Hoje é {dia}. Streak: {streak} dias.",
        f"Horas disponíveis hoje: {available_hours}h.",
    ]

    if events:
        lines.append(f"Reuniões hoje ({len(events)}):")
        for e in events[:4]:
            lines.append(f"  - {e['title']} ({e['start']}, {e['duration_minutes']}min)")

    lines.append("Tarefas prioritárias de hoje:")
    for i, t in enumerate(top_tasks, 1):
        prazo = t.deadline.strftime("%d/%m") if t.deadline else "sem prazo"
        boss = " ⚔️ BOSS FIGHT" if t.is_boss_fight else ""
        lines.append(f"{i}. {t.title} (prazo: {prazo}){boss}")

    if daily_victory:
        lines.append(
            f"Vitória do dia (tarefa #1): {daily_victory.title}. "
            "Instrua Pedro que se fizer só essa, o dia valeu."
        )

    if jira_issues:
        lines.append(f"Jira In Progress ({len(jira_issues)} issues):")
        for issue in jira_issues[:3]:
            lines.append(f"  - [{issue.jira_key}] {issue.summary} ({issue.status})")

    lines.append("Formato WhatsApp, sem markdown, máx 3-4 linhas por bloco.")
    return "\n".join(lines)


async def _get_streak(db) -> int:
    result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak = result.scalar_one_or_none()
    return streak.streak_count if streak else 0
