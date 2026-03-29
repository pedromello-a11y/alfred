"""
weekly_report.py — domingo 10:00
Envia relatório semanal "onde foi meu tempo":
% por projeto/categoria, dia mais produtivo, fator de estimativa, comparação vs semana anterior.
Também checa F12: se nada de alta importância foi feito em 14 dias, alerta Pedro.
"""
from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Task
from app.services import memory_manager, task_manager, whapi_client


async def run() -> None:
    try:
        report = await memory_manager.generate_weekly_report()
        await whapi_client.send_message(settings.pedro_phone, report)
        logger.info("Weekly report sent.")

        # F12: alertar se nada de alta importância foi concluído nos últimos 14 dias
        await _check_high_importance()

    except Exception as exc:
        logger.error("weekly_report.run failed: {}", exc)


async def _check_high_importance() -> None:
    """F12: Se nenhuma tarefa com importance >= 3 foi concluída em 14 dias, alertar."""
    try:
        async with AsyncSessionLocal() as db:
            if not await task_manager.can_send_proactive(db):
                logger.info("F12 check skipped — proactive budget exhausted.")
                return

            cutoff = datetime.combine(date.today() - timedelta(days=14), datetime.min.time())
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.importance >= 3)
                .where(Task.completed_at >= cutoff)
                .limit(1)
            )
            found = result.scalar_one_or_none()
            if found:
                return  # há trabalho de alto impacto recente

            # Verificar se há tarefas de alto impacto no backlog
            pending_result = await db.execute(
                select(Task)
                .where(Task.status == "pending")
                .where(Task.importance >= 3)
                .limit(1)
            )
            pending_high = pending_result.scalar_one_or_none()
            if not pending_high:
                return  # sem tarefas importantes no backlog

            msg = (
                "Você não trabalhou em nada de alto impacto há 2 semanas. "
                "Quer reservar 1h pra isso essa semana?"
            )
            await whapi_client.send_message(settings.pedro_phone, msg)
            await task_manager.increment_proactive_count(db)
            logger.info("F12 high-importance alert sent.")

    except Exception as exc:
        logger.error("weekly_report._check_high_importance failed: {}", exc)
