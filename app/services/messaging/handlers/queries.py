"""
Handlers para consultas: tarefas ativas, contexto de projeto.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_manager
from app.services.messaging.handlers.utils import build_jira_active_lines, status_label


async def handle_active_tasks(db: AsyncSession) -> str:
    tasks = list(await task_manager.get_active_tasks(db))
    recent_done = list(await task_manager.get_recently_done(db))

    if not tasks:
        jira_lines = await build_jira_active_lines(db)
        if jira_lines:
            response = ["Ativas agora (Jira/cache):"]
            response.extend(jira_lines[:5])
            if recent_done:
                response.append("\nResolvidas por último:")
                response.extend([f"- {t.title}" for t in recent_done[:3]])
            response.append("\nSe você me mandar um resumo de status, eu materializo isso na sua lista ativa.")
            return "\n".join(response)
        return "Você não tem tarefas ativas agora."

    top_now = tasks[:3]
    lines = ["Ativas agora:"]
    for i, task in enumerate(top_now, 1):
        extra = []
        if task.estimated_minutes:
            extra.append(f"~{task.estimated_minutes}min")
        if task.priority:
            extra.append(f"p{task.priority}")
        if task.deadline:
            extra.append(f"prazo {task.deadline.strftime('%d/%m')}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{i}. {task.title} — {status_label(task.status)}{suffix}")

    if len(tasks) > 3:
        lines.append("\nEm acompanhamento:")
        for task in tasks[3:7]:
            lines.append(f"- {task.title} — {status_label(task.status)}")

    if recent_done:
        lines.append("\nResolvidas por último:")
        for task in recent_done[:3]:
            lines.append(f"- {task.title}")

    lines.append("\nQual dessas está na sua mão agora?")
    return "\n".join(lines)


async def handle_context_query(project_name: str, db: AsyncSession) -> str:
    from sqlalchemy import select as _select
    from app.models import Memory as _Memory, Task as _Task

    tasks_result = await db.execute(
        _select(_Task)
        .where(_Task.title.ilike(f"%{project_name}%"))
        .order_by(_Task.created_at.desc())
        .limit(10)
    )
    tasks = tasks_result.scalars().all()
    mem_result = await db.execute(
        _select(_Memory)
        .where(_Memory.content.ilike(f"%{project_name}%"))
        .where(_Memory.superseded == False)
        .order_by(_Memory.period_start.desc())
        .limit(3)
    )
    memories = mem_result.scalars().all()
    if not tasks and not memories:
        return f"Não encontrei contexto para '{project_name}'. Tenta com outra palavra-chave."
    lines = [f"Contexto do projeto/tarefa '{project_name}':"]
    for t in tasks:
        status_icon = "✅" if t.status == "done" else "⏳"
        lines.append(f"{status_icon} {t.title} ({t.status})")
        if t.notes:
            lines.append(f"   📝 {t.notes[:200]}")
    if memories:
        lines.append("\nNo histórico:")
        for m in memories[:2]:
            for sentence in m.content.split(". "):
                if project_name.lower() in sentence.lower():
                    lines.append(f"  - {sentence.strip()[:150]}")
                    break
    return "\n".join(lines)
