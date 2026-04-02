"""Auto-limpeza de backlog pessoal.

- Tasks pessoais com 30+ dias → pergunta "Ainda importa?"
- Tasks pessoais com 60+ dias sem ação → arquiva automaticamente
- DumpItems com 90+ dias e status=unknown → arquiva
- Roda toda segunda às 8h30
"""
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import DumpItem, Task
from app.services import task_manager, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            cutoff_30 = now - timedelta(days=30)
            cutoff_60 = now - timedelta(days=60)
            cutoff_90 = now - timedelta(days=90)

            # 60+ days: archive automatically
            result_60 = await db.execute(
                select(Task)
                .where(Task.category == "personal")
                .where(Task.status == "pending")
                .where(Task.created_at < cutoff_60)
            )
            stale_60 = list(result_60.scalars().all())
            archived_count = 0
            for task in stale_60:
                task.status = "cancelled"
                task.notes = (
                    f"{task.notes or ''}\n"
                    f"[{now.strftime('%Y-%m-%d %H:%M')}] Arquivada automaticamente após 60+ dias."
                ).strip()
                archived_count += 1

            # 30–60 days: ask user
            result_30 = await db.execute(
                select(Task)
                .where(Task.category == "personal")
                .where(Task.status == "pending")
                .where(Task.created_at < cutoff_30)
                .where(Task.created_at >= cutoff_60)
            )
            stale_30 = list(result_30.scalars().all())

            # Dumps with 90+ days and unknown status: archive
            dump_result = await db.execute(
                select(DumpItem)
                .where(DumpItem.status == "unknown")
                .where(DumpItem.created_at < cutoff_90)
            )
            stale_dumps = list(dump_result.scalars().all())
            for dump in stale_dumps:
                dump.status = "archived"

            await db.commit()

            if not (stale_30 or archived_count > 0 or stale_dumps):
                return

            if not await task_manager.can_send_proactive(db):
                return

            lines = ["🧹 *Limpeza de backlog pessoal*\n"]

            if archived_count > 0:
                lines.append(
                    f"Arquivei *{archived_count}* tarefa(s) pessoal(is) com 60+ dias sem ação."
                )

            if stale_30:
                lines.append(f"\nEstas estão há 30+ dias paradas:")
                for task in stale_30[:5]:
                    age = (now - task.created_at).days if task.created_at else "?"
                    lines.append(f"- *{task.title}* ({age} dias)")
                lines.append("\nAinda importam? Me diz quais manter ou descartar.")

            if stale_dumps:
                lines.append(
                    f"\nArquivei {len(stale_dumps)} dump(s) não categorizado(s) com 90+ dias."
                )

            await whapi_client.send_message(settings.pedro_phone, "\n".join(lines))
            await task_manager.increment_proactive_count(db)

            logger.info(
                "Backlog cleanup: {} archived, {} stale-30, {} dumps archived",
                archived_count,
                len(stale_30),
                len(stale_dumps),
            )
    except Exception as exc:
        logger.error("backlog_cleanup.run failed: {}", exc)
