"""
morning_briefing.py
  - run_preview():      07:00 — aviso leve do dia
  - run_full():         09:00 — briefing definitivo com plano completo, Jira, GCal e vitória do dia
  - run_ritual_nudge(): 09:30 — nudge se Pedro não respondeu ao ritual de início
"""
import asyncio
import json
import random
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Message, Streak, Task
from app.services import brain, gcal_client, jira_client, task_manager, whapi_client


# ---------------------------------------------------------------------------
# Daily quest pool
# ---------------------------------------------------------------------------

DAILY_QUESTS = [
    {"id": "personal_done",    "desc": "Completar uma tarefa pessoal hoje"},
    {"id": "early_start",      "desc": "Começar em menos de 15min após o briefing"},
    {"id": "win_before_lunch", "desc": "Concluir a vitória do dia antes do meio-dia"},
    {"id": "no_postpone",      "desc": "Não adiar nenhuma tarefa hoje"},
    {"id": "boss_attack",      "desc": "Atacar um boss fight"},
]


async def run_preview() -> None:
    """07:00 — preview leve: quantas reuniões e tarefas prioritárias. Conta no budget."""
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                logger.info("Morning preview skipped — proactive budget exhausted.")
                return

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
            await task_manager.increment_proactive_count(db)
            logger.info("Morning preview sent.")
    except Exception as exc:
        logger.error("morning_briefing.run_preview failed: {}", exc)


async def run_full() -> None:
    """09:00 — briefing definitivo: plano do dia salvo em daily_plans."""
    try:
        async with AsyncSessionLocal() as db:
            # Dia de respiro: não enviar plano, apenas mensagem de bom descanso
            day_off = await task_manager.get_setting("day_off_tomorrow", "false", db=db)
            if day_off == "true":
                await task_manager.set_setting("day_off_tomorrow", "false", db)
                await task_manager.set_setting("day_off_bonus_active", "true", db)
                await whapi_client.send_message(
                    settings.pedro_phone,
                    "Bom descanso! 🌿 Hoje é seu dia de respiro. Sem plano, sem cobranças.\n"
                    "Quando voltar amanhã, vai estar com 1.5x XP em tudo. 💪"
                )
                logger.info("Day off morning message sent.")
                return

            tasks = await task_manager.get_pending(db)
            streak = await _get_streak(db)

            # Resetar budget de interrupções proativas do dia
            await task_manager.reset_proactive_count(db)

            # Checar inatividade 48h → ativar modo crise automaticamente
            await _check_inactivity_crisis(db)

            # Checar modo crise
            crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
            if crisis_mode == "true":
                easiest = (
                    min(tasks, key=lambda t: (t.priority or 5, t.estimated_minutes or 999))
                    if tasks else None
                )
                await _send_crisis_briefing(db, easiest)
                return

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
                if not t.is_boss_fight:
                    if (t.estimated_minutes and t.estimated_minutes > 120) or t.times_planned >= 3:
                        t.is_boss_fight = True
                        logger.info("Boss fight detectado: {}", t.title)
            await db.commit()

            # Buscar dados Jira e GCal em paralelo
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

            # Gerar daily quest secreta (nunca revelar agora)
            quest = random.choice(DAILY_QUESTS)
            await task_manager.set_setting("daily_quest", json.dumps(quest), db)
            logger.info("Daily quest set: {}", quest["id"])

            # Resetar flags do dia
            await task_manager.set_setting("unstuck_used_today", "false", db)
            await task_manager.set_setting("rest_xp_granted_today", "false", db)
            await task_manager.set_setting("ritual_answered", "false", db)
            await task_manager.set_setting("prestige_offered", "false", db)
            await task_manager.set_setting("awaiting_ritual_response", "true", db)

            context = _build_briefing_context(
                top, streak, today, jira_issues, available_hours, events, daily_victory
            )
            briefing_text = await brain.generate_briefing(context, db=db)

            # Ritual de início — pergunta ao final
            if top:
                briefing_text += "\n\nQual dessas quer atacar primeiro? Responde *1*, *2* ou *3*."

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


async def run_ritual_nudge() -> None:
    """
    09:30 — nudge se Pedro não respondeu ao ritual de início (30min após briefing).
    Conta no budget de interrupções proativas.
    """
    try:
        async with AsyncSessionLocal() as db:
            ritual_answered = await task_manager.get_setting("ritual_answered", "false", db=db)
            if ritual_answered == "true":
                return

            # Checar budget de interrupções
            if not await task_manager.can_send_proactive(db):
                logger.info("Ritual nudge (30min) skipped — proactive budget exhausted.")
                return

            # Verificar se houve mensagem inbound depois das 9h
            cutoff = datetime.now(timezone.utc).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            result = await db.execute(
                select(Message)
                .where(Message.direction == "inbound")
                .where(Message.created_at >= cutoff)
                .limit(1)
            )
            if result.scalar_one_or_none():
                await task_manager.set_setting("ritual_answered", "true", db)
                return

            # Sem resposta — nudge
            victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
            tarefa = "sua tarefa principal"
            if victory_id:
                t_result = await db.execute(
                    select(Task).where(Task.id == victory_id)
                )
                victory = t_result.scalar_one_or_none()
                if victory:
                    tarefa = victory.title

            msg = (
                f"Ainda esperando. 👀 Qual vai ser?\n"
                f"Se tiver difícil de começar, foca só em *{tarefa}* por 5min."
            )
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
            logger.info("Ritual nudge sent (30min, no response).")
    except Exception as exc:
        logger.error("morning_briefing.run_ritual_nudge failed: {}", exc)


async def run_ritual_nudge_1h() -> None:
    """
    10:00 — 1h sem resposta: mensagem mais suave "Dia difícil? Me conta."
    Após esse envio, parar de insistir (awaiting_ritual_response=false).
    """
    try:
        async with AsyncSessionLocal() as db:
            ritual_answered = await task_manager.get_setting("ritual_answered", "false", db=db)
            if ritual_answered == "true":
                return

            awaiting = await task_manager.get_setting("awaiting_ritual_response", "false", db=db)
            if awaiting != "true":
                return

            # Checar budget de interrupções
            if not await task_manager.can_send_proactive(db):
                logger.info("Ritual nudge (1h) skipped — proactive budget exhausted.")
                # Parar de insistir mesmo sem enviar
                await task_manager.set_setting("awaiting_ritual_response", "false", db)
                return

            # Verificar se houve mensagem inbound depois das 9h
            cutoff = datetime.now(timezone.utc).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            result = await db.execute(
                select(Message)
                .where(Message.direction == "inbound")
                .where(Message.created_at >= cutoff)
                .limit(1)
            )
            if result.scalar_one_or_none():
                await task_manager.set_setting("ritual_answered", "true", db)
                await task_manager.set_setting("awaiting_ritual_response", "false", db)
                return

            msg = "Dia difícil? Me conta. 🤝"
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
            # Parar de insistir após 10h
            await task_manager.set_setting("awaiting_ritual_response", "false", db)
            logger.info("Ritual nudge sent (1h, final).")
    except Exception as exc:
        logger.error("morning_briefing.run_ritual_nudge_1h failed: {}", exc)


# ---------------------------------------------------------------------------
# Modo crise — briefing simplificado
# ---------------------------------------------------------------------------

async def _send_crisis_briefing(db, easiest_task) -> None:
    titulo = easiest_task.title if easiest_task else "descansar"
    msg = (
        f"Os últimos dias foram difíceis. Hoje só uma coisa: *{titulo}*. Sem pressão.\n"
        f"Se quiser conversar, pode falar."
    )
    await whapi_client.send_message(settings.pedro_phone, msg)
    crisis_since = await task_manager.get_setting("crisis_since", db=db)
    logger.info("Crisis briefing sent (since {}).", crisis_since)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

    # F13 — Sugestão adaptativa de pessoal por tipo de dia
    weekday = today.weekday()  # 0=segunda, 4=sexta
    n_work = sum(1 for t in top_tasks if t.category == "work")
    if available_hours < 4 or n_work >= 3:
        personal_hint = "Dia pesado: se incluir tarefa pessoal, só 1 do tipo 'quick' (< 15min)."
    elif weekday == 4:  # sexta
        personal_hint = "Sexta à tarde: bom momento para uma tarefa pessoal de 'logistics' (organização, compras, etc.)."
    elif available_hours >= 6:
        personal_hint = "Dia leve: pode incluir 1 tarefa pessoal de 'project' (algo mais elaborado)."
    else:
        personal_hint = "Inclua até 1 tarefa pessoal nos espaços livres."
    lines.append(personal_hint)

    lines.append(
        "Formato: distribua as tarefas em blocos de tempo com horários reais, "
        "considerando as horas disponíveis e as reuniões acima. "
        "Use buffer de 15min entre projetos diferentes. "
        "Exemplo de formato:\n"
        "  09:00-11:00 → [tarefa] (foco criativo)\n"
        "  11:00-11:15 → transição\n"
        "  11:15-12:30 → [tarefa] (energia média)\n"
        "Sem markdown (###, ```). Máx 4-5 blocos. WhatsApp."
    )
    return "\n".join(lines)


async def _check_inactivity_crisis(db) -> None:
    """Ativa modo crise se Pedro não respondeu nenhuma mensagem nas últimas 48h."""
    crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
    if crisis_mode == "true":
        return  # já está em crise

    cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
    result = await db.execute(
        select(Message)
        .where(Message.direction == "inbound")
        .where(Message.created_at >= cutoff_48h)
        .limit(1)
    )
    if result.scalar_one_or_none() is None:
        await task_manager.set_setting("crisis_mode", "true", db)
        await task_manager.set_setting("crisis_since", date.today().isoformat(), db)
        logger.warning("Crisis mode activated: 48h inactivity detected.")


async def _get_streak(db) -> int:
    result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak = result.scalar_one_or_none()
    return streak.streak_count if streak else 0
