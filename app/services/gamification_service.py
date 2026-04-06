"""Serviço de gamificação — XP, streak, loot, achievements, multiplier."""
from __future__ import annotations

import random
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlayerStat, Task


# ── XP / Level ─────────────────────────────────────────────────────────────

def calculate_level(total_xp: int) -> int:
    """Level baseado em progressão exponencial: level N requer N*100 XP."""
    level = 1
    xp_needed = 100
    remaining = total_xp
    while remaining >= xp_needed:
        remaining -= xp_needed
        level += 1
        xp_needed = level * 100
    return level


def xp_progress_in_level(total_xp: int, level: int) -> tuple[int, int]:
    """Retorna (xp_atual_no_nivel, xp_para_proxima_subida)."""
    spent = sum(i * 100 for i in range(1, level))
    current_in_level = total_xp - spent
    needed = level * 100
    return current_in_level, needed


def calculate_points(task: Task) -> int:
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
    from app.services.settings_service import get_setting
    result = await db.execute(select(PlayerStat).where(PlayerStat.attribute == attribute))
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
    stat.level = calculate_level(stat.xp)
    await db.commit()
    logger.info(
        "XP granted: {} +{} XP (prestige x{:.1f}, day_off x{:.1f}) → total {} (nível {})",
        attribute, final_xp, prestige_mult, day_off_mult, stat.xp, stat.level,
    )
    return stat


# ── Loot ───────────────────────────────────────────────────────────────────

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


# ── Multiplier ──────────────────────────────────────────────────────────────

async def _update_multiplier(db: AsyncSession) -> float:
    from dateutil.parser import parse as parse_dt
    from app.services.settings_service import get_setting, set_setting
    now = datetime.now(timezone.utc)

    last_str = await get_setting("last_task_completed_at", db=db)
    if last_str:
        try:
            last_dt = parse_dt(last_str)
            if last_dt.tzinfo is not None:
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
            logger.warning("Erro ao calcular multiplier; usando 1.0")
            mult = 1.0
    else:
        mult = 1.0

    await set_setting("current_multiplier", str(mult), db)
    await set_setting("last_task_completed_at", now.isoformat(), db)
    return mult


# ── award_task_completion — ponto de entrada principal ──────────────────────

async def award_task_completion(task: Task, db: AsyncSession) -> tuple[int, str]:
    """Concede XP, rola loot e retorna (xp_concedido, mensagem)."""
    from app.services.settings_service import get_setting, set_setting

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

    return final_xp, xp_msg + loot_msg
