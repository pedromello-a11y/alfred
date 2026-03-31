from datetime import date

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DailyPlan, Task
from app.services import alfred_brain_unified, task_manager, whapi_client
from app.services.tomorrow_board import build_tomorrow_board


async def run_preview() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return

            tasks = list(await task_manager.get_active_tasks(db))[:3]
            board = await build_tomorrow_board(db)
            due_count = len(board.get("dueTomorrow", []))
            lines = [f"Bom dia. Hoje você acorda com *{len(tasks)} focos ativos*." ]
            if due_count:
                lines.append(f"Tem *{due_count}* item(ns) importantes batendo amanhã.")
            if tasks:
                lines.append("Primeiros focos:")
                for i, task in enumerate(tasks, 1):
                    lines.append(f"{i}. {task.title}")
            lines.append("Briefing completo às 9h.")
            await whapi_client.send_message(settings.pedro_phone, "\n".join(lines))
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("morning_briefing_rebuild.run_preview failed: {}", exc)


async def run_full() -> None:
    try:
        async with AsyncSessionLocal() as db:
            tasks = list(await task_manager.get_active_tasks(db))
            board = await build_tomorrow_board(db)
            top = tasks[:3]
            victory = top[0] if top else None
            if victory:
                await task_manager.set_setting("daily_victory_task_id", str(victory.id), db)

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
            for i, task in enumerate(top, 1):
                prazo = task.deadline.strftime("%d/%m %H:%M") if task.deadline else "sem prazo"
                prompt_lines.append(f"{i}. {task.title} (prazo {prazo})")
            prompt_lines.append("")
            prompt_lines.append("Tomorrow board:")
            for item in board.get("dueTomorrow", [])[:3]:
                prompt_lines.append(f"- amanhã: {item['title']} ({item['deadline']})")

            briefing_text = await alfred_brain_unified.generate_text(
                "\n".join(prompt_lines),
                db=db,
                max_tokens=400,
                temperature=0.3,
                call_type="morning_briefing_rebuild",
            )
            if not briefing_text:
                briefing_text = "Vitória do dia: escolhe seu foco principal e me responde com *1*, *2* ou *3*."

            plan = DailyPlan(
                plan_date=date.today(),
                plan_content=briefing_text,
                tasks_planned={"ids": [str(t.id) for t in top]},
            )
            db.add(plan)
            await db.commit()
            await whapi_client.send_message(settings.pedro_phone, briefing_text)
    except Exception as exc:
        logger.error("morning_briefing_rebuild.run_full failed: {}", exc)


async def run_ritual_nudge() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            answered = await task_manager.get_setting("ritual_answered", "false", db=db)
            if answered == "true":
                return
            victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
            title = "sua tarefa principal"
            if victory_id:
                result = await db.execute(select(Task).where(Task.id == victory_id))
                task = result.scalar_one_or_none()
                if task:
                    title = task.title
            await whapi_client.send_message(settings.pedro_phone, f"Qual vai ser? Se estiver difícil, começa 5min em *{title}*.")
            await task_manager.increment_proactive_count(db)
    except Exception as exc:
        logger.error("morning_briefing_rebuild.run_ritual_nudge failed: {}", exc)


async def run_ritual_nudge_1h() -> None:
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                return
            answered = await task_manager.get_setting("ritual_answered", "false", db=db)
            if answered == "true":
                return
            await whapi_client.send_message(settings.pedro_phone, "Dia difícil? Me conta.")
            await task_manager.increment_proactive_count(db)
            await task_manager.set_setting("ritual_answered", "true", db)
    except Exception as exc:
        logger.error("morning_briefing_rebuild.run_ritual_nudge_1h failed: {}", exc)
