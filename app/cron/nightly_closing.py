"""Fechamento noturno completo — streak, achievements, XP, memória."""
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Achievement, DailyPlan, Streak, Task
from app.services import brain, task_manager, whapi_client
from app.services.time_utils import today_brt
from app.services.tomorrow_board import build_tomorrow_board


async def run() -> None:
    """Job principal de fechamento noturno."""
    try:
        async with AsyncSessionLocal() as db:
            today = today_brt()
            start = datetime.combine(today, datetime.min.time())

            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= start)
            )
            done_today = list(result.scalars().all())
            pending = list(await task_manager.get_active_tasks(db))
            points = sum(task_manager.calculate_points(t) for t in done_today)

            streak_count = await _update_streak(db, today, len(done_today), points)

            plan_result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan = plan_result.scalar_one_or_none()
            if plan:
                plan.consolidated = True
                plan.score = points
                plan.tasks_completed = {"ids": [str(t.id) for t in done_today]}

            tomorrow = await build_tomorrow_board(db)
            due_tomorrow = tomorrow.get("dueTomorrow", [])
            next_hint = (
                due_tomorrow[0]["title"]
                if due_tomorrow
                else (pending[0].title if pending else "nenhum foco definido")
            )

            closing_context = (
                f"Fechamento do dia {today.strftime('%d/%m')}:\n"
                f"- {len(done_today)} tarefas concluídas\n"
                f"- {len(pending)} tarefas abertas\n"
                f"- {points} pontos\n"
                f"- Streak: {streak_count} dias\n"
                f"- Primeiro foco amanhã: {next_hint}\n\n"
                f"Gere uma mensagem curta de fechamento para o Pedro. "
                f"Reconheça o que foi feito, mencione o streak, "
                f"e sugira o primeiro foco de amanhã. Máximo 6 linhas."
            )

            try:
                closing_text = await brain._call(
                    closing_context,
                    max_tokens=300,
                    temperature=0.3,
                    call_type="nightly_closing",
                    db=db,
                    include_history=False,
                )
            except Exception:
                closing_text = (
                    f"Fechamento: {len(done_today)} concluídas, "
                    f"{len(pending)} abertas, {points} pontos. "
                    f"Streak: {streak_count} dias. "
                    f"Amanhã: *{next_hint}*."
                )

            await _check_achievements(db, done_today, streak_count, today)

            await task_manager.reset_proactive_count(db)
            await task_manager.set_setting("ritual_answered", "false", db)
            await task_manager.set_setting("rest_xp_granted_today", "false", db)
            await task_manager.set_setting("unstuck_used_today", "false", db)

            await db.commit()
            await whapi_client.send_message(settings.pedro_phone, closing_text)
            logger.info(
                "Nightly closing done: {} tasks, {} points, streak {}",
                len(done_today), points, streak_count,
            )

    except Exception as exc:
        logger.error("nightly_closing.run failed: {}", exc)


async def _update_streak(db, today: date, tasks_done: int, points: int) -> int:
    yesterday = today - timedelta(days=1)

    yesterday_result = await db.execute(
        select(Streak).where(Streak.streak_date == yesterday)
    )
    yesterday_streak = yesterday_result.scalar_one_or_none()
    prev_count = yesterday_streak.streak_count if yesterday_streak else 0

    new_count = prev_count + 1 if tasks_done > 0 else 0

    today_result = await db.execute(
        select(Streak).where(Streak.streak_date == today)
    )
    streak = today_result.scalar_one_or_none()
    if streak:
        streak.tasks_completed = tasks_done
        streak.points = points
        streak.streak_count = new_count
    else:
        streak = Streak(
            streak_date=today,
            tasks_completed=tasks_done,
            points=points,
            streak_count=new_count,
        )
        db.add(streak)

    await db.flush()
    return new_count


async def _check_achievements(
    db, done_today: list, streak_count: int, today: date
) -> None:
    now = datetime.now(timezone.utc)

    async def _unlock(code: str) -> bool:
        result = await db.execute(
            select(Achievement).where(Achievement.code == code)
        )
        ach = result.scalar_one_or_none()
        if ach and ach.unlocked_at is None:
            ach.unlocked_at = now
            logger.info("Achievement unlocked: {}", code)
            return True
        return False

    # first_blood: tarefa concluída antes das 12h UTC (~9h BRT)
    for task in done_today:
        if task.completed_at and task.completed_at.hour < 12:
            await _unlock("first_blood")
            break

    # early_bird: streak >= 5 dias
    if streak_count >= 5:
        await _unlock("early_bird")

    # perfect_day: 100% do plano concluído
    plan_result = await db.execute(
        select(DailyPlan).where(DailyPlan.plan_date == today)
    )
    plan = plan_result.scalar_one_or_none()
    if plan and plan.tasks_planned:
        planned_ids = set(plan.tasks_planned.get("ids", []))
        done_ids = {str(t.id) for t in done_today}
        if planned_ids and planned_ids.issubset(done_ids):
            await _unlock("perfect_day")

    # sniper: tarefa em menos da metade do tempo estimado
    for task in done_today:
        if (
            task.estimated_minutes
            and task.actual_minutes
            and task.actual_minutes < task.estimated_minutes / 2
        ):
            await _unlock("sniper")
            break

    # archaeologist: tarefa criada há mais de 30 dias
    cutoff_30d = now - timedelta(days=30)
    for task in done_today:
        if task.created_at and task.created_at < cutoff_30d:
            await _unlock("archaeologist")
            break

    # ghost: dia produtivo sem usar destravamento
    unstuck_used = await task_manager.get_setting("unstuck_used_today", "false", db=db)
    if len(done_today) >= 3 and unstuck_used != "true":
        await _unlock("ghost")

    # combo_x3: 3 tarefas concluídas em sequência sem pausa >10min
    if len(done_today) >= 3:
        sorted_done = sorted(
            [t for t in done_today if t.completed_at],
            key=lambda t: t.completed_at,
        )
        for i in range(len(sorted_done) - 2):
            t1 = sorted_done[i].completed_at
            t2 = sorted_done[i + 1].completed_at
            t3 = sorted_done[i + 2].completed_at
            if (
                t1 and t2 and t3
                and (t2 - t1).total_seconds() < 600
                and (t3 - t2).total_seconds() < 600
            ):
                await _unlock("combo_x3")
                break

    # phoenix: retomou após 3+ dias de inatividade
    recent_streaks_result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(5)
    )
    streaks_list = list(recent_streaks_result.scalars().all())
    if len(streaks_list) >= 2 and streaks_list[0].tasks_completed > 0:
        gap_days = sum(1 for s in streaks_list[1:] if s.tasks_completed == 0)
        if gap_days >= 3:
            await _unlock("phoenix")

    await db.flush()
