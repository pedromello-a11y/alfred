"""
nightly_closing.py — 21:00 seg-sex
1. Conta tarefas concluídas no dia
2. Calcula pontuação ponderada por esforço (calculate_points)
3. Atualiza streak
4. Gera memória diária via brain.consolidate_memory
5. Salva em memories + atualiza daily_plan como consolidated
6. Checa conquistas não desbloqueadas → envia surpresa se nova
7. Revela daily quest se cumprida → +50 XP bônus
8. Checa prestige (todos os 5 atributos >= nível 20)
9. Rest XP: se inativo 15min+ após pausa sugerida, concede Recovery XP
10. Envia resumo
"""
from datetime import date, datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import Achievement, DailyPlan, PlayerStat, Streak, Task
from app.services import brain, task_manager, whapi_client


async def run() -> None:
    try:
        async with AsyncSessionLocal() as db:
            today = date.today()
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

            # Tarefas concluídas hoje
            result = await db.execute(
                select(Task)
                .where(Task.status == "done")
                .where(Task.completed_at >= today_start)
            )
            done_today = result.scalars().all()

            # Tarefas ainda pendentes
            pending = await task_manager.get_pending(db)

            # Pontuação ponderada
            pontos = sum(task_manager.calculate_points(t) for t in done_today)

            # Atualizar/criar streak
            streak_count = await _update_streak(db, today, len(done_today), pontos)

            # Consolidar memória diária via memory_manager
            from app.services import memory_manager
            await memory_manager.consolidate_daily(db)

            # Marcar daily_plan como consolidated
            plan_result = await db.execute(
                select(DailyPlan).where(DailyPlan.plan_date == today)
            )
            plan = plan_result.scalar_one_or_none()
            if plan:
                plan.consolidated = True
                plan.tasks_completed = {"ids": [str(t.id) for t in done_today]}
                plan.score = pontos
            await db.commit()

            # Gerar e enviar fechamento principal
            closing_context = _build_closing_context(done_today, pending, pontos, streak_count)
            closing_text = await brain.generate_closing(closing_context, db=db)
            await whapi_client.send_message(settings.pedro_phone, closing_text)
            logger.info("Nightly closing sent. Done={}, points={}, streak={}.", len(done_today), pontos, streak_count)

            # --- Conquistas (surpresa) ---
            await _check_achievements(db, today, done_today, streak_count)

            # --- Daily quest reveal ---
            await _reveal_daily_quest(db, done_today)

            # --- Prestige check ---
            await _check_prestige(db)

            # --- Dia de respiro (streak >= 7) ---
            await _check_day_off(db, streak_count)

            # --- Rest XP ---
            await _grant_rest_xp(db)

            # --- Modo crise: 3 dias consecutivos com score zero ---
            await _check_score_zero_crisis(db, today, pontos)

            # --- Decaimento de backlog (F11) ---
            await _check_backlog_decay(db, today)

    except Exception as exc:
        logger.error("nightly_closing.run failed: {}", exc)


# ---------------------------------------------------------------------------
# Streak
# ---------------------------------------------------------------------------

async def _update_streak(db, today: date, n_done: int, pontos: int) -> int:
    result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    last = result.scalar_one_or_none()

    yesterday = today - timedelta(days=1)
    if last and last.streak_date == yesterday and n_done > 0:
        new_count = last.streak_count + 1
    elif n_done > 0:
        new_count = 1
    else:
        new_count = 0

    streak = Streak(
        streak_date=today,
        tasks_completed=n_done,
        points=pontos,
        streak_count=new_count,
    )
    db.add(streak)
    await db.flush()
    return new_count


# ---------------------------------------------------------------------------
# Contexto para brain
# ---------------------------------------------------------------------------

def _build_closing_context(done: list, pending: list, pontos: int, streak: int) -> str:
    return (
        f"Gere o fechamento do dia para Pedro.\n"
        f"Concluídas hoje: {len(done)} tarefas\n"
        f"Ficaram pendentes: {len(pending)} tarefas\n"
        f"Pontos: {pontos}\n"
        f"Streak: {streak} dias\n"
        f"Formato WhatsApp, sem markdown."
    )


# ---------------------------------------------------------------------------
# Conquistas
# ---------------------------------------------------------------------------

_ACHIEVEMENT_CHECKS = {
    "first_blood": "Primeira tarefa do dia concluída antes das 9h",
    "combo_x3": "3 tarefas concluídas em sequência sem pausa >10min",
    "slayer": "Boss fight derrotado na primeira tentativa",
    "early_bird": "5 dias seguidos começando antes das 9h",
    "perfect_day": "100% do plano diário concluído",
    "phoenix": "Retomou produtividade após 3+ dias de inatividade",
    "sniper": "Tarefa concluída em menos da metade do tempo estimado",
    "balanced": "Tarefas work E personal no mesmo dia",
    "archaeologist": "Concluiu tarefa do backlog com mais de 30 dias",
    "ghost": "Dia inteiro produtivo sem pedir ajuda de destravamento",
}


async def _check_achievements(db, today: date, done_today: list, streak_count: int) -> None:
    """Verifica condições de cada achievement não desbloqueado. Envia surpresa se novo."""
    result = await db.execute(
        select(Achievement).where(Achievement.unlocked_at == None)  # noqa: E711
    )
    locked = result.scalars().all()
    if not locked:
        return

    today_start = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
    unlocked_codes: list[str] = []

    for ach in locked:
        if await _evaluate_achievement(ach.code, db, today, today_start, done_today, streak_count):
            ach.unlocked_at = datetime.utcnow()
            unlocked_codes.append(ach.code)
            logger.info("Achievement unlocked: {}", ach.code)

    if unlocked_codes:
        await db.commit()
        for code in unlocked_codes:
            result2 = await db.execute(select(Achievement).where(Achievement.code == code))
            ach = result2.scalar_one_or_none()
            if ach:
                msg = f"🏆 Conquista desbloqueada: *{ach.name}*\n_{ach.description}_"
                await whapi_client.send_message(settings.pedro_phone, msg)


async def _evaluate_achievement(
    code: str, db, today: date, today_start: datetime, done_today: list, streak_count: int
) -> bool:
    """Retorna True se a conquista foi conquistada hoje."""
    from app.models import Message

    if code == "first_blood":
        # Primeira tarefa concluída antes das 9h
        nine_am = today_start.replace(hour=9)
        return any(
            t.completed_at and t.completed_at.replace(tzinfo=timezone.utc) < nine_am
            for t in done_today
        )

    elif code == "combo_x3":
        # 3 tarefas em sequência com <10min entre elas
        if len(done_today) < 3:
            return False
        sorted_tasks = sorted(
            [t for t in done_today if t.completed_at],
            key=lambda t: t.completed_at
        )
        combo = 1
        for i in range(1, len(sorted_tasks)):
            delta = (sorted_tasks[i].completed_at - sorted_tasks[i - 1].completed_at).total_seconds()
            if delta < 600:
                combo += 1
                if combo >= 3:
                    return True
            else:
                combo = 1
        return False

    elif code == "slayer":
        # Boss fight concluído hoje
        return any(t.is_boss_fight for t in done_today)

    elif code == "early_bird":
        # 5 dias seguidos com streak >= 5 (proxy: streak atual >= 5)
        return streak_count >= 5

    elif code == "perfect_day":
        # Todas as tarefas planejadas foram concluídas
        plan_result = await db.execute(
            select(DailyPlan).where(DailyPlan.plan_date == today)
        )
        plan = plan_result.scalar_one_or_none()
        if not plan or not plan.tasks_planned:
            return False
        planned_ids = set(plan.tasks_planned.get("ids", []))
        done_ids = {str(t.id) for t in done_today}
        return bool(planned_ids) and planned_ids.issubset(done_ids)

    elif code == "phoenix":
        # Voltou a produzir após 3+ dias sem tarefas
        from sqlalchemy import func
        three_days_ago = today - timedelta(days=3)
        result = await db.execute(
            select(Streak)
            .where(Streak.streak_date >= three_days_ago)
            .where(Streak.streak_date < today)
        )
        recent = result.scalars().all()
        # Todos os 3 dias anteriores com zero tarefas
        inactive_days = [s for s in recent if s.tasks_completed == 0]
        return len(inactive_days) >= 3 and len(done_today) > 0

    elif code == "sniper":
        # Tarefa concluída em menos da metade do tempo estimado
        return any(
            t.estimated_minutes and t.actual_minutes
            and t.actual_minutes < t.estimated_minutes / 2
            for t in done_today
        )

    elif code == "balanced":
        # Tarefas work E personal no mesmo dia
        cats = {t.category for t in done_today}
        return "work" in cats and "personal" in cats

    elif code == "archaeologist":
        # Concluiu tarefa criada há mais de 30 dias
        thirty_days_ago = datetime.combine(today - timedelta(days=30), datetime.min.time())
        return any(
            t.created_at and t.created_at < thirty_days_ago
            for t in done_today
        )

    elif code == "ghost":
        # Dia produtivo sem acionar unstuck_mode
        unstuck_used = await task_manager.get_setting("unstuck_used_today", "false", db=db)
        return len(done_today) > 0 and unstuck_used != "true"

    return False


# ---------------------------------------------------------------------------
# Daily quest reveal
# ---------------------------------------------------------------------------

async def _reveal_daily_quest(db, done_today: list) -> None:
    """Revela daily quest apenas se foi cumprida. Concede +50 XP bônus."""
    quest_json = await task_manager.get_setting("daily_quest", db=db)
    if not quest_json:
        return

    import json
    try:
        quest = json.loads(quest_json)
    except (json.JSONDecodeError, TypeError):
        return

    quest_id = quest.get("id", "")
    quest_desc = quest.get("desc", "")
    completed = await _check_quest_completed(quest_id, db, done_today)

    if completed:
        # +50 XP bônus em strategy
        stat = await task_manager.grant_xp("strategy", 50, db)
        msg = (
            f"🎲 Daily quest revelada: *{quest_desc}*\n"
            f"Você cumpriu! +50 XP bônus de strategy (nível {stat.level})"
        )
        await whapi_client.send_message(settings.pedro_phone, msg)
        logger.info("Daily quest completed: {}", quest_id)

    # Limpar quest do dia
    await task_manager.set_setting("daily_quest", "", db)


async def _check_quest_completed(quest_id: str, db, done_today: list) -> bool:
    """Verifica se a daily quest foi cumprida."""
    today = date.today()

    if quest_id == "personal_done":
        return any(t.category == "personal" for t in done_today)

    elif quest_id == "early_start":
        # Primeira tarefa concluída em menos de 15min após briefing (09:00)
        briefing_time = datetime.combine(today, datetime.min.time()).replace(
            tzinfo=timezone.utc, hour=9
        )
        cutoff = briefing_time + timedelta(minutes=15)
        return any(
            t.completed_at and t.completed_at.replace(tzinfo=timezone.utc) <= cutoff
            for t in done_today
        )

    elif quest_id == "win_before_lunch":
        # Vitória do dia concluída antes do meio-dia
        victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
        if not victory_id:
            return False
        noon = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc, hour=12)
        return any(
            str(t.id) == victory_id
            and t.completed_at
            and t.completed_at.replace(tzinfo=timezone.utc) < noon
            for t in done_today
        )

    elif quest_id == "no_postpone":
        # Nenhuma tarefa foi adiada hoje (proxy: nenhuma ficou pendente com last_planned = today)
        result = await db.execute(
            select(Task)
            .where(Task.status == "pending")
            .where(Task.last_planned == today)
        )
        rescheduled = result.scalars().all()
        return len(rescheduled) == 0

    elif quest_id == "boss_attack":
        # Tentou um boss fight (concluiu ou está em progresso)
        return any(t.is_boss_fight for t in done_today)

    return False


# ---------------------------------------------------------------------------
# Prestige check
# ---------------------------------------------------------------------------

async def _check_prestige(db) -> None:
    """Se todos os 5 atributos principais >= nível 20, oferecer prestige."""
    _PRESTIGE_ATTRIBUTES = {"craft", "strategy", "life", "willpower", "knowledge"}

    result = await db.execute(
        select(PlayerStat).where(PlayerStat.attribute.in_(_PRESTIGE_ATTRIBUTES))
    )
    stats = result.scalars().all()

    if len(stats) < 5:
        return

    all_maxed = all(s.level >= 20 for s in stats)
    if not all_maxed:
        return

    # Verificar se já ofertou prestige recentemente (evitar spam)
    already_offered = await task_manager.get_setting("prestige_offered", db=db)
    if already_offered == "true":
        return

    await task_manager.set_setting("prestige_offered", "true", db)
    msg = (
        "🌟 *PRESTIGE DISPONÍVEL!*\n"
        "Todos os atributos chegaram ao nível 20!\n"
        "Quer fazer Prestige? Reset pra nível 1 com 1.1x XP permanente.\n"
        "Responde *SIM* pra confirmar."
    )
    await whapi_client.send_message(settings.pedro_phone, msg)
    logger.info("Prestige offered to Pedro.")


# ---------------------------------------------------------------------------
# Rest XP
# ---------------------------------------------------------------------------

async def _grant_rest_xp(db) -> None:
    """
    Se Alfred sugeriu pausa e Pedro ficou inativo 15min+, concede XP de Recovery.
    Proxy: verificar se última mensagem inbound foi há mais de 15min
    e se a última mensagem outbound continha sugestão de pausa.
    """
    from app.models import Message

    now = datetime.now(timezone.utc)
    cutoff_15min = now - timedelta(minutes=15)

    # Última mensagem inbound
    last_inbound = await db.execute(
        select(Message)
        .where(Message.direction == "inbound")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_in = last_inbound.scalar_one_or_none()
    if not last_in:
        return

    # Verificar se Pedro ficou calado por 15min+
    if last_in.created_at.replace(tzinfo=timezone.utc) > cutoff_15min:
        return

    # Verificar se última mensagem outbound sugeria pausa
    last_outbound = await db.execute(
        select(Message)
        .where(Message.direction == "outbound")
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_out = last_outbound.scalar_one_or_none()
    if not last_out:
        return

    _PAUSE_KEYWORDS = ("pausa", "descanso", "coffee break", "descanse", "respira", "break")
    if not any(kw in last_out.content.lower() for kw in _PAUSE_KEYWORDS):
        return

    # Verificar se já concedeu Rest XP hoje
    rest_granted = await task_manager.get_setting("rest_xp_granted_today", db=db)
    if rest_granted == "true":
        return

    stat = await task_manager.grant_xp("recovery", 10, db)
    await task_manager.set_setting("rest_xp_granted_today", "true", db)
    logger.info("Rest XP granted: +10 recovery (nível {})", stat.level)


# ---------------------------------------------------------------------------
# Dia de respiro (G9)
# ---------------------------------------------------------------------------

async def _check_score_zero_crisis(db, today: date, pontos_hoje: int) -> None:
    """Ativa modo crise se 3 dias consecutivos com pontuação zero."""
    crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
    if crisis_mode == "true":
        return  # já está em crise

    three_days_ago = today - timedelta(days=3)
    result = await db.execute(
        select(Streak)
        .where(Streak.streak_date >= three_days_ago)
        .where(Streak.streak_date < today)
        .order_by(Streak.streak_date.desc())
    )
    recent = result.scalars().all()
    zero_days = [s for s in recent if (s.points or 0) == 0]
    if pontos_hoje == 0 and len(zero_days) >= 2:
        await task_manager.set_setting("crisis_mode", "true", db)
        await task_manager.set_setting("crisis_since", today.isoformat(), db)
        logger.warning("Crisis mode activated: 3 consecutive zero-score days.")


async def _check_day_off(db, streak_count: int) -> None:
    """Se streak >= 7, oferecer dia de respiro. Se aceitar: day_off_tomorrow=true."""
    if streak_count < 7:
        return

    # Só oferecer uma vez por ciclo de 7 dias
    day_off_offered = await task_manager.get_setting("day_off_offered", "false", db=db)
    if day_off_offered == "true":
        return

    await task_manager.set_setting("day_off_offered", "true", db)
    msg = (
        f"🎉 *{streak_count} dias seguidos!* Você merece um descanso.\n"
        f"Quer amanhã como dia de respiro? Sem plano, sem cobranças.\n"
        f"Responde *RESPIRO* pra confirmar."
    )
    await whapi_client.send_message(settings.pedro_phone, msg)
    logger.info("Day off offered (streak={}).", streak_count)


# ---------------------------------------------------------------------------
# Decaimento de backlog (F11)
# ---------------------------------------------------------------------------

async def _check_backlog_decay(db, today: date) -> None:
    """
    Verifica tarefas estagnadas no backlog:
    - times_planned >= 3: pergunta se quer quebrar ou cancelar
    - personal + created_at > 30 dias: "ainda importa?"
    - personal + created_at > 60 dias: auto-arquivar silenciosamente
    Limita a 3 notificações por noite para não sobrecarregar.
    """
    thirty_days_ago = datetime.combine(today - timedelta(days=30), datetime.min.time())
    sixty_days_ago = datetime.combine(today - timedelta(days=60), datetime.min.time())
    messages_sent = 0

    # 1. Auto-arquivar tarefas pessoais com >60 dias sem ação
    old_personal_result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.category == "personal")
        .where(Task.created_at < sixty_days_ago)
    )
    old_personal = old_personal_result.scalars().all()
    for t in old_personal:
        t.status = "archived"
        logger.info("Task auto-archived (>60d personal): {}", t.title)
    if old_personal:
        await db.commit()

    # 2. Tarefas com times_planned >= 3: perguntar
    stuck_result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.times_planned >= 3)
        .limit(2)
    )
    stuck = stuck_result.scalars().all()
    for t in stuck:
        if messages_sent >= 3:
            break
        msg = (
            f"'{t.title}' tá há {t.times_planned}x no plano sem sair. "
            f"Quer que eu quebre em partes menores ou cancelamos?"
        )
        await whapi_client.send_message(settings.pedro_phone, msg)
        messages_sent += 1
        logger.info("Backlog decay nudge (times_planned={}): {}", t.times_planned, t.title)

    # 3. Tarefas pessoais com >30 dias: perguntar
    old_30_result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.category == "personal")
        .where(Task.created_at < thirty_days_ago)
        .where(Task.created_at >= sixty_days_ago)  # >60d já foram auto-arquivadas
        .limit(1)
    )
    old_30 = old_30_result.scalars().all()
    for t in old_30:
        if messages_sent >= 3:
            break
        msg = f"'{t.title}' tem mais de 1 mês no backlog. Ainda importa?"
        await whapi_client.send_message(settings.pedro_phone, msg)
        messages_sent += 1
        logger.info("Backlog decay nudge (>30d personal): {}", t.title)
