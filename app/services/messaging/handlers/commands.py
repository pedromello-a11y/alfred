"""
Handlers para comandos diretos: delegate, drop, ritual choice, prestige, day off.
"""
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_manager


async def handle_delegate(raw_text: str, db: AsyncSession | None) -> str:
    if db is None:
        return "Anotado! Pra quem vai delegar?"
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return "Sem tarefas ativas pra delegar."
    task_to_delegate = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_delegate = t
            break
    await task_manager.delegate_task(task_to_delegate.title, "a definir", db)
    return f"*{task_to_delegate.title}* marcada como delegada. Pra quem vai?"


async def handle_drop(raw_text: str, db: AsyncSession | None) -> str:
    if db is None:
        return "Qual tarefa quer remover?"
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return "Sem tarefas ativas."
    task_to_drop = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_drop = t
            break
    await task_manager.drop_task(task_to_drop.title, db)
    return f"*{task_to_drop.title}* removida da lista. Sem penalidade. ✂️"


async def handle_ritual_choice(choice: str, db: AsyncSession) -> str:
    from sqlalchemy import select as _select
    from app.models import DailyPlan, Task as _Task

    await task_manager.set_setting("awaiting_ritual_response", "false", db)
    today = date.today()
    result = await db.execute(_select(DailyPlan).where(DailyPlan.plan_date == today))
    plan = result.scalar_one_or_none()
    if plan and plan.tasks_planned and "ids" in plan.tasks_planned:
        idx = int(choice) - 1
        task_ids = plan.tasks_planned["ids"]
        if 0 <= idx < len(task_ids):
            task_id = task_ids[idx]
            await task_manager.set_setting("daily_victory_task_id", task_id, db)
            t_result = await db.execute(_select(_Task).where(_Task.id == task_id))
            task = t_result.scalar_one_or_none()
            if task:
                return f"Ótimo! Foco em *{task.title}*. Bora! 🎯"
    return f"Opção {choice} registrada. Bora começar! 🎯"


async def handle_prestige_accept(db: AsyncSession) -> str:
    from sqlalchemy import select
    from app.models import PlayerStat

    attrs = ["craft", "strategy", "life", "willpower", "knowledge"]
    result = await db.execute(select(PlayerStat).where(PlayerStat.attribute.in_(attrs)))
    stats = result.scalars().all()
    prestige_num = (stats[0].prestige + 1) if stats else 1
    for stat in stats:
        stat.prestige = prestige_num
        stat.xp = 0
        stat.level = 1
    await task_manager.set_setting("prestige_offered", "false", db)
    await db.commit()
    multiplier = 1 + (prestige_num * 0.1)
    return (
        f"🌟 *PRESTIGE {prestige_num} ATIVADO!*\n"
        f"Todos os atributos resetados para nível 1.\n"
        f"Multiplicador permanente: {multiplier:.1f}x XP.\n"
        f"Nova jornada começa agora. 💪"
    )


async def handle_day_off_accept(db: AsyncSession) -> str:
    await task_manager.set_setting("day_off_tomorrow", "true", db)
    await task_manager.set_setting("day_off_offered", "false", db)
    return "Combinado! 🌿 Amanhã é dia de respiro.\nSem plano, sem cobranças. E na volta: 1.5x XP em tudo. 💪"
