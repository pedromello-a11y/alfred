import random
from datetime import date, datetime
from typing import Sequence

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlayerStat, Settings, Task
from app.services.message_handler import InboundItem


# ---------------------------------------------------------------------------
# Priority score (spec: normalization-priority.md)
# ---------------------------------------------------------------------------

def calculate_priority_score(
    task: Task,
    available_hours: float = 8.0,
    current_streak: int = 0,
    today: date | None = None,
) -> int:
    today = today or date.today()
    score = 0

    if task.deadline:
        deadline_date = task.deadline.date() if isinstance(task.deadline, datetime) else task.deadline
        days_until = (deadline_date - today).days
        if days_until <= 0:
            score += 150
        elif days_until <= 1:
            score += 100
        elif days_until <= 3:
            score += 60
        elif days_until <= 7:
            score += 30

    if task.priority == 1:
        score += 50
    elif task.priority == 2:
        score += 30
    elif task.priority == 3:
        score += 15

    if task.category == "work":
        score += 20

    if task.estimated_minutes:
        if task.estimated_minutes <= available_hours * 60:
            score += 20
        else:
            score -= 10

    if current_streak >= 5:
        score += 10
    elif current_streak >= 3:
        score += 5

    return score


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

_PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}


async def create(item: InboundItem, db: AsyncSession) -> Task:
    priority = _PRIORITY_MAP.get(item.priority_hint or "", None)
    deadline = None
    if item.deadline:
        deadline = datetime.combine(item.deadline, datetime.min.time())

    # F13 — Auto-classificar effort_type por estimated_minutes
    minutes = item.metadata.get("estimated_minutes") if item.metadata else None
    if minutes is None:
        effort_type = "quick"  # default conservador
    elif minutes < 15:
        effort_type = "quick"
    elif minutes <= 60:
        effort_type = "logistics"
    else:
        effort_type = "project"

    task = Task(
        title=item.extracted_title,
        origin=item.origin,
        status="pending",
        priority=priority,
        deadline=deadline,
        category=item.category,
        effort_type=effort_type,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info("Task created: {} (id={})", task.title, task.id)
    return task


async def get_pending(db: AsyncSession) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
    )
    return result.scalars().all()


def calculate_points(task: Task) -> int:
    """Pontuação ponderada por esforço (spec: melhorias.md item 8)."""
    minutes = task.estimated_minutes or 30
    if minutes < 30:
        base = 5
    elif minutes <= 60:
        base = 10
    elif minutes <= 180:
        base = 20
    else:
        base = 35
    if task.deadline and task.completed_at and task.completed_at < task.deadline:
        base = int(base * 1.5)
    return base


async def mark_done(title_fragment: str, db: AsyncSession) -> tuple[Task | None, str]:
    """
    Marca tarefa como concluída.
    Retorna (task, loot_message) onde loot_message é '' se sem loot.
    Após concluir: concede XP, checa multiplier, rola loot (15%).
    """
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.title.ilike(f"%{title_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if not task:
        return None, ""

    task.status = "done"
    task.completed_at = datetime.utcnow()
    await db.commit()
    logger.info("Task done: {} (id={})", task.title, task.id)

    # XP base
    base_xp = calculate_points(task)

    # xp_boost loot ativo: dobrar XP desta tarefa e consumir
    xp_boost = await get_setting("active_loot_xp_boost", "false", db=db)
    if xp_boost == "true":
        base_xp *= 2
        await set_setting("active_loot_xp_boost", "false", db)
        logger.info("xp_boost loot consumed: base_xp doubled")

    # Multiplier de combo
    mult = await _update_multiplier(db)
    if task.is_boss_fight:
        mult = max(mult, 3.0)  # boss fight = mínimo 3x
        logger.info("Boss fight concluído! XP x3: {}", task.title)

    final_xp = int(base_xp * mult)
    attribute = get_attribute(task)
    stat = await grant_xp(attribute, final_xp, db)

    # Limpar day_off_bonus após primeira tarefa do dia de retorno
    day_off_bonus = await get_setting("day_off_bonus_active", "false", db=db)
    if day_off_bonus == "true":
        await set_setting("day_off_bonus_active", "false", db)

    mult_str = f" (x{mult:.1f} multiplier)" if mult > 1.0 else ""
    xp_msg = f"+{final_xp} XP de {attribute}{mult_str} (nível {stat.level})"
    if task.is_boss_fight:
        xp_msg = f"⚔️ Boss fight derrotado! {xp_msg}"

    loot = roll_loot()
    loot_msg = ""
    if loot:
        loot_code, loot_text = loot
        loot_msg = f"\n🎲 Loot drop: {loot_text}"
        await set_setting(f"active_loot_{loot_code}", "true", db)
        logger.info("Loot drop: {}", loot_code)

    return task, xp_msg + loot_msg


async def delegate_task(title_fragment: str, delegated_to: str, db: AsyncSession) -> Task | None:
    """Marca tarefa como delegada."""
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.title.ilike(f"%{title_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task:
        task.status = "delegated"
        task.notes = f"Delegado para: {delegated_to}"
        await db.commit()
        logger.info("Task delegated: {} → {}", task.title, delegated_to)
    return task


async def drop_task(title_fragment: str, db: AsyncSession) -> Task | None:
    """Marca tarefa como dropped (não importa mais)."""
    result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .where(Task.title.ilike(f"%{title_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task:
        task.status = "dropped"
        await db.commit()
        logger.info("Task dropped: {}", task.title)
    return task


# ---------------------------------------------------------------------------
# Gamificação RPG
# ---------------------------------------------------------------------------

def get_attribute(task: Task) -> str:
    """Mapeia tarefa para atributo RPG."""
    title = (task.title or "").lower()
    cat = (task.category or "").lower()

    # Willpower: tarefas adiadas 3+ vezes
    if (task.times_planned or 0) >= 3:
        return "willpower"

    # Craft: produção criativa
    if any(w in title for w in ("vídeo", "video", "edição", "edicao", "render", "motion", "animação", "animacao")):
        return "craft"

    # Knowledge: aprendizado
    if any(w in title for w in ("curso", "estudar", "pesquisa", "aprender", "ler")):
        return "knowledge"

    # Life: pessoal
    if cat == "personal":
        return "life"

    # Strategy: trabalho genérico
    return "strategy"


async def grant_xp(attribute: str, xp_amount: int, db: AsyncSession) -> PlayerStat:
    """Concede XP ao atributo. Aplica prestige multiplier e day_off_bonus. Atualiza level."""
    result = await db.execute(
        select(PlayerStat).where(PlayerStat.attribute == attribute)
    )
    stat = result.scalar_one_or_none()
    if stat is None:
        stat = PlayerStat(attribute=attribute, xp=0, level=1, prestige=0)
        db.add(stat)
        await db.flush()

    # Prestige permanent multiplier: 1 + (prestige * 0.1)
    prestige_mult = 1.0 + (stat.prestige * 0.1) if stat.prestige else 1.0

    # Dia de respiro bonus: 1.5x no dia de retorno
    day_off_bonus = await get_setting("day_off_bonus_active", "false", db=db)
    day_off_mult = 1.5 if day_off_bonus == "true" else 1.0

    final_xp = int(xp_amount * prestige_mult * day_off_mult)
    stat.xp += final_xp
    stat.level = max(1, stat.xp // 100)
    await db.commit()
    logger.info("XP granted: {} +{} XP (prestige x{:.1f}, day_off x{:.1f}) → total {} (nível {})",
                attribute, final_xp, prestige_mult, day_off_mult, stat.xp, stat.level)
    return stat


LOOT_TABLE = [
    ("coffee_break", "☕ Coffee break! Pausa de 10min merecida."),
    ("xp_boost", "⚡ XP Boost! Próxima tarefa vale 2x."),
    ("skip_ticket", "🎫 Skip Ticket! Pode adiar 1 tarefa sem culpa."),
    ("reroll", "🔄 Reroll! Pode trocar sua próxima prioridade."),
]


def roll_loot() -> tuple[str, str] | None:
    """15% de chance de loot drop."""
    if random.random() < 0.15:
        return random.choice(LOOT_TABLE)
    return None


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

async def get_setting(key: str, default: str | None = None, db: AsyncSession | None = None) -> str | None:
    if db is None:
        return default
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else default


async def set_setting(key: str, value: str, db: AsyncSession) -> None:
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        db.add(Settings(key=key, value=value))
    else:
        setting.value = value
    await db.commit()


# ---------------------------------------------------------------------------
# Multiplier de combo
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Budget de interrupções proativas
# ---------------------------------------------------------------------------

_PROACTIVE_BUDGET_KEY = "proactive_messages_today"
_PROACTIVE_LIMIT_KEY = "proactive_budget_limit"


async def can_send_proactive(db: AsyncSession) -> bool:
    """Retorna True se ainda há budget para mensagem proativa hoje. Limite padrão: 3."""
    limit = int(await get_setting(_PROACTIVE_LIMIT_KEY, "3", db=db) or "3")
    current = int(await get_setting(_PROACTIVE_BUDGET_KEY, "0", db=db) or "0")
    return current < limit


async def increment_proactive_count(db: AsyncSession) -> int:
    """Incrementa contador de mensagens proativas e retorna novo valor."""
    current = int(await get_setting(_PROACTIVE_BUDGET_KEY, "0", db=db) or "0")
    new_val = current + 1
    await set_setting(_PROACTIVE_BUDGET_KEY, str(new_val), db)
    return new_val


async def reset_proactive_count(db: AsyncSession) -> None:
    """Reseta contador diário de interrupções proativas (chamar no morning_briefing)."""
    await set_setting(_PROACTIVE_BUDGET_KEY, "0", db)


async def _update_multiplier(db: AsyncSession) -> float:
    """Atualiza multiplier de combo. <10min entre tarefas = +0.5x (cap 3.0)."""
    from dateutil.parser import parse as parse_dt
    now = datetime.utcnow()

    last_str = await get_setting("last_task_completed_at", db=db)
    if last_str:
        try:
            last_dt = parse_dt(last_str)
            if last_dt.tzinfo is not None:
                from datetime import timezone
                now_aware = now.replace(tzinfo=timezone.utc)
                elapsed = (now_aware - last_dt).total_seconds()
            else:
                elapsed = (now - last_dt).total_seconds()

            if elapsed < 600:  # <10min → incrementar combo
                mult = float(await get_setting("current_multiplier", "1.0", db=db))
                mult = min(mult + 0.5, 3.0)
            elif elapsed > 1800:  # >30min → resetar
                mult = 1.0
            else:  # 10-30min → manter atual
                mult = float(await get_setting("current_multiplier", "1.0", db=db))
        except Exception:
            mult = 1.0
    else:
        mult = 1.0

    await set_setting("current_multiplier", str(mult), db)
    await set_setting("last_task_completed_at", now.isoformat(), db)
    return mult
