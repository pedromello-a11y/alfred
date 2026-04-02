"""Morning briefing — preview, briefing completo e nudges."""
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Task
from app.services import brain, task_manager, whapi_client
from app.services.time_utils import today_brt
from app.services.tomorrow_board import build_tomorrow_board


async def run_preview() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            tasks = list(await task_manager.get_active_tasks(db))[:3]
            board = await build_tomorrow_board(db)
            due_count = len(board.get("dueTomorrow", []))
            lines = [f"Bom dia. Hoje você acorda com *{len(tasks)} focos ativos*."]
            if due_count:
                lines.append(f"Tem *{due_count}* item(ns) importantes batendo amanhã.")
            if tasks:
                lines.append("Primeiros focos:")
                for i, t in enumerate(tasks, 1):
                    lines.append(f"{i}. {t.title}")
            lines.append("Briefing completo às 9h.")
            await whapi_client.send_message(settings.pedro_phone, "\n".join(lines))
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("morning_briefing.run_preview failed: {}", exc)


async def run_full() -> None:
    try:
        async with AsyncSessionLocal() as db:
            today = today_brt()
            tasks = list(await task_manager.get_active_tasks(db))
            board = await build_tomorrow_board(db)
            top = tasks[:3]
            victory = top[0] if top else None
            if victory:
                await task_manager.set_setting(
                    "daily_victory_task_id", str(victory.id), db
                )
            await task_manager.set_setting("awaiting_ritual_response", "true", db)

            # Bug 1.2: increment times_planned and detect boss fights
            for t in top:
                t.times_planned = (t.times_planned or 0) + 1
                t.last_planned = today
                if t.times_planned >= 3 and not t.is_boss_fight:
                    t.is_boss_fight = True
                    logger.info("Boss fight detected: {} (planned {} times)", t.title, t.times_planned)
            await db.flush()

            # Feature 2.6: generate daily quest
            from app.services.daily_quest import generate_daily_quest
            await generate_daily_quest(db)
            await task_manager.set_setting(
                "briefing_sent_at", datetime.now(timezone.utc).isoformat(), db
            )
            await task_manager.set_setting("tasks_postponed_today", "0", db)

            prompt_lines = [
                "Gere o briefing do dia para o Pedro em WhatsApp.",
                "Regras:",
                "- curto e objetivo",
                "- máximo 12 linhas",
                "- definir vitória do dia",
                "- terminar perguntando qual foco ele quer atacar primeiro",
                "",
                "Tarefas mais importantes:",
            ]
            for i, t in enumerate(top, 1):
                prazo = (
                    t.deadline.strftime("%d/%m %H:%M") if t.deadline else "sem prazo"
                )
                prompt_lines.append(f"{i}. {t.title} (prazo {prazo})")
            prompt_lines.append("")
            prompt_lines.append("Tomorrow board:")
            for item in board.get("dueTomorrow", [])[:3]:
                prompt_lines.append(
                    f"- amanhã: {item['title']} ({item['deadline']})"
                )

            briefing_text = await brain._call(
                "\n".join(prompt_lines),
                max_tokens=400,
                temperature=0.3,
                call_type="morning_briefing",
                db=db,
                include_history=False,
            )
            if not briefing_text:
                briefing_text = (
                    "Vitória do dia: escolhe seu foco principal "
                    "e me responde com *1*, *2* ou *3*."
                )

            plan = DailyPlan(
                plan_date=today,
                plan_content=briefing_text,
                tasks_planned={"ids": [str(t.id) for t in top]},
            )
            db.add(plan)
            await db.commit()
            await whapi_client.send_message(settings.pedro_phone, briefing_text)
    except Exception as exc:
        logger.error("morning_briefing.run_full failed: {}", exc)


async def run_ritual_nudge() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            answered = await task_manager.get_setting(
                "ritual_answered", "false", db=db
            )
            if answered == "true":
                return
            victory_id = await task_manager.get_setting(
                "daily_victory_task_id", db=db
            )
            title = "sua tarefa principal"
            if victory_id:
                result = await db.execute(select(Task).where(Task.id == victory_id))
                task = result.scalar_one_or_none()
                if task:
                    title = task.title
            await whapi_client.send_message(
                settings.pedro_phone,
                f"Qual vai ser? Se estiver difícil, começa 5min em *{title}*.",
            )
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("morning_briefing.run_ritual_nudge failed: {}", exc)


async def run_ritual_nudge_1h() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            answered = await task_manager.get_setting(
                "ritual_answered", "false", db=db
            )
            if answered == "true":
                return
            await whapi_client.send_message(
                settings.pedro_phone, "Dia difícil? Me conta."
            )
            await task_manager.increment_proactive_count(db)
            await task_manager.set_setting("ritual_answered", "true", db)
    except Exception as exc:
        logger.error("morning_briefing.run_ritual_nudge_1h failed: {}", exc)
