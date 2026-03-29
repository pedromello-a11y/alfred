import random
from datetime import date, datetime
from typing import Sequence

from loguru import logger
from sqlalchemy import or_, select
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
_OPEN_STATUSES = ("pending", "in_progress")


async def create(item: InboundItem, db: AsyncSession) -> Task:
    priority = _PRIORITY_MAP.get(item.priority_hint or "", None)
    deadline = None
    if item.deadline:
        deadline = datetime.combine(item.deadline, datetime.min.time())

    minutes = item.metadata.get("estimated_minutes") if item.metadata else None
    if minutes is None:
        effort_type = "quick"
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


async def get_active_tasks(db: AsyncSession) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last(), Task.created_at.desc())
    )
    return result.scalars().all()


async def get_recently_done(db: AsyncSession, limit: int = 5) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .order_by(Task.completed_at.desc().nullslast(), Task.created_at.desc())
        .limit(limit)
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


async def find_task_by_fragment(
    title_fragment: str,
    db: AsyncSession,
    open_only: bool = True,
) -> Task | None:
    query = select(Task)
    if open_only:
        query = query.where(Task.status.in_(_OPEN_STATUSES))
    query = (
        query.where(Task.title.ilike(f"%{title_fragment}%"))
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last(), Task.created_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def search_tasks_by_keywords(
    keywords: list[str],
    db: AsyncSession,
    open_only: bool = True,
    limit: int = 10,
) -> Sequence[Task]:
    cleaned = [k.strip() for k in keywords if len(k.strip()) >= 3]
    if not cleaned:
        return []

    clauses = [Task.title.ilike(f"%{kw}%") for kw in cleaned]
    query = select(Task)
    if open_only:
        query = query.where(Task.status.in_(_OPEN_STATUSES))
    query = query.where(or_(*clauses)).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


async def update_task_status(
    task: Task,
    new_status: str,
    db: AsyncSession,
    note: str | None = None,
) -> Task:
    task.status = new_status
    if new_status == "done":
        task.completed_at = datetime.utcnow()
    if note:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        extra = f"[{timestamp}] {note.strip()}"
        task.notes = f"{task.notes}\n{extra}" if task.notes else extra
    await db.commit()
    await db.refresh(task)
    logger.info("Task status updated: {} -> {}", task.title, new_status)
    return task


async def mark_done(title_fragment: str, db: AsyncSession) -> tuple[Task | None, str]:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
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

    base_xp = calculate_points(task)

    xp_boost = await get_setting("active_loot_xp_boost", "false", db=db)
    if xp_boost == "true":
        base_xp *= 2
        await set_setting("active_loot_xp_boost", "false", db)
        logger.info("xp_boost loot consumed: base_xp doubled")

    mult = await _update_multiplier(db)
    if task.is_boss_fight:
        mult = max(mult, 3.0)
        logger.info("Boss fight concluído! XP x3: {}", task.title)

    final_xp = int(base_xp * mult)
    attribute = get_attribute(task)
    stat = await grant_xp(attribute, final_xp, db)

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
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
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
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
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
    title = (task.title or "").lower()
    cat = (task.category or "").lower()

    if (task.times_planned or 0) >= 3:
        return "willpower"

    if any(w in title for w in ("vídeo", "video", "edição", "edicao", "render", "motion", "animação", "animacao")):
        return "craft"

    if any(w in title for w in ("curso", "estudar", "pesquisa", "aprender", "ler")):
        return "knowledge"

    if cat == "personal":
        return "life"

    return "strategy"


async def grant_xp(attribute: str, xp_amount: int, db: AsyncSession) -> PlayerStat:
    result = await db.execute(
        select(PlayerStat).where(PlayerStat.attribute == attribute)
    )
    stat = result.scalar_one_or_none()
    if stat is None:
        stat = PlayerStat(attribute=attribute, xp=0, level=1, prestige=0)
        db.add(stat)
        await db.flush()

    prestige_mult = 1.0 + (stat.prestige * 0.1) if stat.prestige else 1.0

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
    limit = int(await get_setting(_PROACTIVE_LIMIT_KEY, "3", db=db) or "3")
    current = int(await get_setting(_PROACTIVE_BUDGET_KEY, "0", db=db) or "0")
    return current < limit


async def increment_proactive_count(db: AsyncSession) -> int:
    current = int(await get_setting(_PROACTIVE_BUDGET_KEY, "0", db=db) or "0")
    new_val = current + 1
    await set_setting(_PROACTIVE_BUDGET_KEY, str(new_val), db)
    return new_val


async def reset_proactive_count(db: AsyncSession) -> None:
    await set_setting(_PROACTIVE_BUDGET_KEY, "0", db)


async def _update_multiplier(db: AsyncSession) -> float:
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

            if elapsed < 600:
                mult = float(await get_setting("current_multiplier", "1.0", db=db))
                mult = min(mult + 0.5, 3.0)
            elif elapsed > 1800:
                mult = 1.0
            else:
                mult = float(await get_setting("current_multiplier", "1.0", db=db))
        except Exception:
            mult = 1.0
    else:
        mult = 1.0

    await set_setting("current_multiplier", str(mult), db)
    await set_setting("last_task_completed_at", now.isoformat(), db)
    return mult
