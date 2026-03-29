"""
system_review.py — 21:30 seg-sex (primeiros 30 dias)
Meta-análise diária do Alfred: o que funcionou, o que não funcionou, sugestão e 1 pergunta.
Desativado automaticamente após 30 dias (setting system_review_active=false).
"""
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Message, Task
from app.services import brain, task_manager, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            # Verificar se ainda está ativo (primeiros 30 dias)
            active = await task_manager.get_setting("system_review_active", "true", db=db)
            if active != "true":
                logger.info("System review disabled (system_review_active=false).")
                return

            # Verificar se já passou dos 30 dias desde a primeira execução
            first_run_str = await task_manager.get_setting("system_review_first_run", db=db)
            today = date.today()
            if first_run_str:
                try:
                    first_run = date.fromisoformat(first_run_str)
                    if (today - first_run).days >= 30:
                        await task_manager.set_setting("system_review_active", "false", db)
                        logger.info("System review disabled after 30 days.")
                        return
                except ValueError:
                    pass
            else:
                await task_manager.set_setting("system_review_first_run", today.isoformat(), db)

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            # Tarefas do dia
            done_result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = done_result.scalars().all()

            planned_ids_count = 0
            from app.models import DailyPlan
            plan_result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan = plan_result.scalar_one_or_none()
            if plan and plan.tasks_planned:
                planned_ids_count = len(plan.tasks_planned.get("ids", []))

            # Mensagens do dia
            msgs_result = await db.execute(
                select(Message)
                .where(Message.created_at >= today_start)
                .order_by(Message.created_at)
            )
            msgs = msgs_result.scalars().all()
            n_inbound = sum(1 for m in msgs if m.direction == "inbound")
            n_outbound = sum(1 for m in msgs if m.direction == "outbound")

            # Flags do dia
            unstuck_used = await task_manager.get_setting("unstuck_used_today", "false", db=db)
            crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)

            # Erros: mensagens outbound com classification='error' são raras; usar proxy: checar logs
            # (sem acesso direto a logs, deixar como placeholder no prompt)

            context = (
                f"Analise o dia do Alfred (assistente) como sistema, não do Pedro.\n"
                f"Data: {today.strftime('%d/%m/%Y')}\n"
                f"Tarefas planejadas: {planned_ids_count}\n"
                f"Tarefas concluídas: {len(done_today)}\n"
                f"Mensagens recebidas de Pedro: {n_inbound}\n"
                f"Mensagens enviadas pelo Alfred: {n_outbound}\n"
                f"Modo destravamento ativado: {unstuck_used}\n"
                f"Modo crise ativo: {crisis_mode}\n\n"
                f"Gere uma revisão concisa do funcionamento do sistema hoje. Formato:\n"
                f"📋 Revisão do sistema — {today.strftime('%d/%m')}\n"
                f"O que funcionou: [concreto, 1 linha]\n"
                f"O que não funcionou: [concreto, 1 linha]\n"
                f"Sugestão de ajuste: [acionável, 1 linha]\n"
                f"Pergunta pro Pedro: [1 pergunta curta]\n\n"
                f"Máx 5 linhas. Sem markdown (###, **). WhatsApp."
            )

            review_text = await brain.generate_closing(context, db=db)
            await whapi_client.send_message(settings.pedro_phone, review_text)
            logger.info("System review sent for {}.", today)

    except Exception as exc:
        logger.error("system_review.run failed: {}", exc)
