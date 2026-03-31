import random
import re
import unicodedata
from datetime import date, datetime
from typing import TYPE_CHECKING, Sequence

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlayerStat, Settings, Task

if TYPE_CHECKING:
    from app.services.message_handler import InboundItem


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


_PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}
_OPEN_STATUSES = ("pending", "in_progress")
_SYSTEM_HINTS = (
    "audio nao funciona",
    "audio do sistema",
    "bug do audio",
    "ajustes do sistema",
    "sistema alfred",
    "bug audio alfred",
)
_MATCH_STOPWORDS = {
    "de",
    "da",
    "do",
    "das",
    "dos",
    "para",
    "pra",
    "com",
    "sem",
    "uma",
    "um",
    "na",
    "no",
    "em",
    "por",
    "que",
    "e",
    "o",
    "a",
    "as",
    "os",
}
_GENERIC_MATCH_TOKENS = {
    "video",
    "videos",
    "motion",
    "task",
    "tarefa",
    "tarefas",
    "demanda",
    "demandas",
    "projeto",
    "projetos",
    "ajustar",
    "fazer",
    "reuniao",
    "reunioes",
}
_PROJECT_ALIAS_RULES = [
    ({"spark"}, {"spark"}),
    ({"fire", "26"}, {"fire", "fire26", "f26"}),
    ({"fire"}, {"fire"}),
    ({"galaxy"}, {"galaxy"}),
    ({"hotmart"}, {"hotmart"}),
    ({"3k"}, {"3k"}),
    ({"marcom"}, {"marcom"}),
]

_CANONICAL_TITLE_RULES = [
    (["motion avisos"], "Spark | Motion Avisos"),
    (["motion aviso"], "Spark | Motion Avisos"),
    (["avisos do spark"], "Spark | Motion Avisos"),
    (["spark motion avisos"], "Spark | Motion Avisos"),
    (["spark motion aviso"], "Spark | Motion Avisos"),
    (["countdown"], "Spark | Countdown"),
    (["coutndown"], "Spark | Countdown"),
    (["screensaver"], "Spark | Screensaver"),
    (["legendas", "cavazza"], "Padrão de legendas para Cavazza"),
    (["turntable", "cosmos", "2"], "Turntable do Cosmos 2"),
    (["video de abertura"], "Vídeo de Abertura / FIRE"),
    (["video abertura"], "Vídeo de Abertura / FIRE"),
    (["abertura fire"], "Vídeo de Abertura / FIRE"),
    (["projeto da 3k"], "Vídeo de Abertura / FIRE"),
    (["3k", "abertura"], "Vídeo de Abertura / FIRE"),
    (["galaxy", "video", "abertura"], "Vídeo de Abertura / FIRE"),
]


def normalize_task_title(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_title_prefixes(value: str) -> str:
    text = (value or "").strip()
    patterns = [
        r"(?i)^outra\s+demanda\s+(e\s+do|do|da)?\s*",
        r"(?i)^demanda\s+nova[:\-\s]*",
        r"(?i)^e\s+demanda[,:\-\s]*",
        r"(?i)^eh\s+demanda[,:\-\s]*",
        r"(?i)^tenho\s+",
        r"(?i)^preciso\s+",
        r"(?i)^separe\s+assim\s+",
        r"(?i)^consultar\s+viabilidade\s+de\s+fazer\s+(um|uma)\s+",
        r"(?i)^consultar\s+viabilidade\s+de\s+",
        r"(?i)^levantar\s+referencias\s+e\s+propostas\s+pra\s+reuniao\s+do\s+",
        r"(?i)^me\s+diga\s+",
    ]
    changed = True
    while changed:
        changed = False
        for pattern in patterns:
            new_text = re.sub(pattern, "", text).strip(" :-–—")
            if new_text != text:
                text = new_text
                changed = True
    return text.strip()


def canonicalize_task_title(value: str) -> str:
    cleaned = _strip_title_prefixes(value)
    normalized = normalize_task_title(cleaned)
    if not normalized:
        return cleaned or value
    for required_terms, canonical in _CANONICAL_TITLE_RULES:
        if all(term in normalized for term in required_terms):
            return canonical
    return cleaned.strip()


def is_system_task_title(value: str) -> bool:
    normalized = normalize_task_title(value)
    if not normalized:
        return False
    return any(hint in normalized for hint in _SYSTEM_HINTS)


def _split_task_title_parts(value: str) -> tuple[str | None, str]:
    text = (value or "").strip()
    if "|" in text:
        left, right = text.split("|", 1)
        project = left.strip()
        task_name = right.strip()
        if project and task_name:
            return project, task_name
    return None, text


def _meaningful_tokens(value: str) -> set[str]:
    normalized = normalize_task_title(value)
    tokens = set()
    for token in normalized.split():
        if token in _MATCH_STOPWORDS:
            continue
        if len(token) >= 3 or any(ch.isdigit() for ch in token):
            tokens.add(token)
    return tokens


def _project_tokens(value: str) -> set[str]:
    project, _ = _split_task_title_parts(canonicalize_task_title(value))
    source = project or value
    tokens = _meaningful_tokens(source)
    expanded = set(tokens)
    for required, aliases in _PROJECT_ALIAS_RULES:
        if required.issubset(tokens):
            expanded.update(aliases)
    return expanded


def _task_body_tokens(value: str) -> set[str]:
    _, body = _split_task_title_parts(canonicalize_task_title(value))
    return _meaningful_tokens(body)


def _task_match_score(query: str, candidate: str) -> int:
    query_canonical = canonicalize_task_title(query)
    candidate_canonical = canonicalize_task_title(candidate)
    query_norm = normalize_task_title(query_canonical)
    candidate_norm = normalize_task_title(candidate_canonical)
    if not query_norm or not candidate_norm:
        return 0
    if query_norm == candidate_norm:
        return 200

    score = 0
    if query_norm in candidate_norm:
        score += 90
    elif candidate_norm in query_norm:
        score += 60

    query_project = _project_tokens(query_canonical)
    candidate_project = _project_tokens(candidate_canonical)
    if query_project and candidate_project:
        project_overlap = query_project & candidate_project
        if project_overlap:
            score += 25 + (len(project_overlap) * 5)
        else:
            score -= 20

    _, query_body = _split_task_title_parts(query_canonical)
    _, candidate_body = _split_task_title_parts(candidate_canonical)
    query_body_norm = normalize_task_title(query_body or query_canonical)
    candidate_body_norm = normalize_task_title(candidate_body or candidate_canonical)
    if query_body_norm and query_body_norm in candidate_body_norm:
        score += 45
    elif candidate_body_norm and candidate_body_norm in query_body_norm and len(candidate_body_norm) >= 8:
        score += 15

    query_tokens = _task_body_tokens(query_canonical)
    candidate_tokens = _task_body_tokens(candidate_canonical)
    overlap = query_tokens & candidate_tokens
    if overlap:
        score += len(overlap) * 14
        if query_tokens and overlap == query_tokens:
            score += 20
        rare_overlap = [token for token in overlap if token not in _GENERIC_MATCH_TOKENS]
        score += len(rare_overlap) * 4
    else:
        score -= 5

    if len(query_tokens) == 1 and len(overlap) == 1:
        token = next(iter(query_tokens))
        if token in _GENERIC_MATCH_TOKENS:
            score -= 10
        else:
            score += 8

    if overlap and not [token for token in overlap if token not in _GENERIC_MATCH_TOKENS] and not (query_project & candidate_project):
        score -= 12

    return score


def _is_viable_task_match(query: str, candidate: str, score: int) -> bool:
    query_tokens = _task_body_tokens(query)
    overlap = query_tokens & _task_body_tokens(candidate)
    if score >= 90:
        return True
    if len(overlap) >= 2 and score >= 22:
        return True
    if query_tokens and overlap == query_tokens and score >= 18:
        return True
    if len(query_tokens) == 1 and len(overlap) == 1 and score >= 26:
        token = next(iter(query_tokens))
        return token not in _GENERIC_MATCH_TOKENS
    return False


def titles_look_similar(a: str, b: str) -> bool:
    score = _task_match_score(a, b)
    return _is_viable_task_match(a, b, score)


def _dedupe_tasks_by_canonical_title(tasks: Sequence[Task]) -> list[Task]:
    deduped: dict[str, Task] = {}
    for task in tasks:
        key = normalize_task_title(canonicalize_task_title(task.title or ""))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = task
            continue
        current_rank = 1 if task.status == "in_progress" else 2 if task.status == "pending" else 3
        existing_rank = 1 if existing.status == "in_progress" else 2 if existing.status == "pending" else 3
        if current_rank < existing_rank:
            deduped[key] = task
            continue
        if existing_rank == current_rank and (task.created_at or datetime.min) > (existing.created_at or datetime.min):
            deduped[key] = task
    return list(deduped.values())


async def create(item: "InboundItem", db: AsyncSession) -> Task:
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
        title=canonicalize_task_title(item.extracted_title),
        origin=item.origin,
        status="pending",
        priority=priority,
        deadline=deadline,
        category=item.category,
        effort_type=effort_type,
        estimated_minutes=minutes,
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


async def get_active_tasks(db: AsyncSession, include_system: bool = False) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last(), Task.created_at.desc())
    )
    tasks = result.scalars().all()
    if not include_system:
        tasks = [t for t in tasks if t.category not in ("backlog", "system") and not is_system_task_title(t.title or "")]
    return _dedupe_tasks_by_canonical_title(tasks)


async def get_recent_tasks(db: AsyncSession, limit: int = 50, include_system: bool = False) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .order_by(Task.completed_at.desc().nullslast(), Task.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    if not include_system:
        tasks = [t for t in tasks if t.category not in ("backlog", "system") and not is_system_task_title(t.title or "")]
    return _dedupe_tasks_by_canonical_title(tasks)


async def get_recently_done(db: AsyncSession, limit: int = 5, include_system: bool = False) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .order_by(Task.completed_at.desc().nullslast(), Task.created_at.desc())
        .limit(limit)
    )
    tasks = result.scalars().all()
    if not include_system:
        tasks = [t for t in tasks if t.category not in ("backlog", "system") and not is_system_task_title(t.title or "")]
    return _dedupe_tasks_by_canonical_title(tasks)


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


async def _candidate_tasks_for_matching(
    db: AsyncSession,
    *,
    include_closed: bool,
    include_system: bool,
    limit: int = 120,
) -> Sequence[Task]:
    result = await db.execute(
        select(Task)
        .order_by(Task.completed_at.desc().nullslast(), Task.created_at.desc())
        .limit(limit)
    )
    tasks = list(result.scalars().all())
    if not include_closed:
        tasks = [task for task in tasks if task.status in _OPEN_STATUSES]
    if not include_system:
        tasks = [task for task in tasks if task.category not in ("backlog", "system") and not is_system_task_title(task.title or "")]
    return _dedupe_tasks_by_canonical_title(tasks)


async def find_task_by_fragment(title_fragment: str, db: AsyncSession, open_only: bool = True) -> Task | None:
    return await find_task_by_title_like(title_fragment, db, include_closed=not open_only, include_system=False)


async def find_task_by_title_like(title: str, db: AsyncSession, include_closed: bool = True, include_system: bool = False) -> Task | None:
    candidates = await _candidate_tasks_for_matching(db, include_closed=include_closed, include_system=include_system)
    best_task: Task | None = None
    best_score = 0
    for task in candidates:
        score = _task_match_score(title, task.title or "")
        if not _is_viable_task_match(title, task.title or "", score):
            continue
        if score > best_score:
            best_task = task
            best_score = score
            continue
        if score == best_score and best_task is not None:
            if (task.created_at or datetime.min) > (best_task.created_at or datetime.min):
                best_task = task
    return best_task


async def upsert_task_from_context(
    title: str,
    db: AsyncSession,
    *,
    status: str = "pending",
    category: str = "work",
    note: str | None = None,
    estimated_minutes: int | None = None,
) -> Task:
    title = canonicalize_task_title(title)
    existing = await find_task_by_title_like(title, db, include_closed=True, include_system=(category == "system"))
    if existing:
        if note:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            extra = f"[{timestamp}] {note.strip()}"
            existing.notes = f"{existing.notes}\n{extra}" if existing.notes else extra
        if estimated_minutes and not existing.estimated_minutes:
            existing.estimated_minutes = estimated_minutes
        if category:
            existing.category = category
        if status in _OPEN_STATUSES:
            existing.completed_at = None
        elif status == "done":
            existing.completed_at = datetime.utcnow()
        existing.status = status
        await db.commit()
        await db.refresh(existing)
        logger.info("Context task upsert matched existing: {} -> {}", existing.title, status)
        return existing

    task = Task(
        title=title[:500],
        origin="manual",
        status=status,
        category=category,
        effort_type="project",
        estimated_minutes=estimated_minutes,
    )
    if note:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        task.notes = f"[{timestamp}] {note.strip()}"
    if status == "done":
        task.completed_at = datetime.utcnow()
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info("Context task upsert created: {} -> {}", task.title, status)
    return task


async def search_tasks_by_keywords(keywords: list[str], db: AsyncSession, open_only: bool = True, limit: int = 10) -> Sequence[Task]:
    cleaned = [canonicalize_task_title(k.strip()) for k in keywords if len(k.strip()) >= 3]
    if not cleaned:
        return []
    clauses = [Task.title.ilike(f"%{kw}%") for kw in cleaned]
    query = select(Task)
    if open_only:
        query = query.where(Task.status.in_(_OPEN_STATUSES))
    query = query.where(or_(*clauses)).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


async def update_task_status(task: Task, new_status: str, db: AsyncSession, note: str | None = None, category: str | None = None) -> Task:
    task.status = new_status
    if category:
        task.category = category
    if new_status == "done":
        task.completed_at = datetime.utcnow()
    elif new_status in _OPEN_STATUSES:
        task.completed_at = None
    if note:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        extra = f"[{timestamp}] {note.strip()}"
        task.notes = f"{task.notes}\n{extra}" if task.notes else extra
    await db.commit()
    await db.refresh(task)
    logger.info("Task status updated: {} -> {}", task.title, new_status)
    return task


async def rename_most_recent_active_task(new_title: str, db: AsyncSession) -> Task | None:
    tasks = list(await get_active_tasks(db, include_system=True))
    if not tasks:
        return None
    task = tasks[0]
    task.title = canonicalize_task_title(new_title)
    await db.commit()
    await db.refresh(task)
    logger.info("Task renamed: {} (id={})", task.title, task.id)
    return task


async def mark_done(title_fragment: str, db: AsyncSession) -> tuple[Task | None, str]:
    task = await find_task_by_title_like(title_fragment, db, include_closed=False, include_system=True)
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
    task = await find_task_by_title_like(title_fragment, db, include_closed=False, include_system=True)
    if task:
        task.status = "delegated"
        task.notes = f"Delegado para: {delegated_to}"
        await db.commit()
        logger.info("Task delegated: {} → {}", task.title, delegated_to)
    return task


async def drop_task(title_fragment: str, db: AsyncSession) -> Task | None:
    task = await find_task_by_title_like(title_fragment, db, include_closed=False, include_system=True)
    if task:
        task.status = "dropped"
        await db.commit()
        logger.info("Task dropped: {}", task.title)
    return task


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
    stat.level = max(1, stat.xp // 100)
    await db.commit()
    logger.info("XP granted: {} +{} XP (prestige x{:.1f}, day_off x{:.1f}) → total {} (nível {})", attribute, final_xp, prestige_mult, day_off_mult, stat.xp, stat.level)
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
