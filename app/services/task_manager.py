import re
import unicodedata
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Sequence

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import ACTIVE_STATUSES
from app.models import Settings, Task

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
_OPEN_STATUSES = ACTIVE_STATUSES
_SYSTEM_HINTS = (
    "audio nao funciona",
    "audio do sistema",
    "bug do audio",
    "ajustes do sistema",
    "sistema alfred",
    "bug audio alfred",
)

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


def titles_look_similar(a: str, b: str) -> bool:
    na = normalize_task_title(canonicalize_task_title(a))
    nb = normalize_task_title(canonicalize_task_title(b))
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    wa = set(na.split())
    wb = set(nb.split())
    return len(wa & wb) >= 2


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


async def find_task_by_fragment(title_fragment: str, db: AsyncSession, open_only: bool = True) -> Task | None:
    canonical_fragment = canonicalize_task_title(title_fragment)
    query = select(Task)
    if open_only:
        query = query.where(Task.status.in_(_OPEN_STATUSES))
    query = (
        query.where(Task.title.ilike(f"%{canonical_fragment}%"))
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last(), Task.created_at.desc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def find_task_by_title_like(title: str, db: AsyncSession, include_closed: bool = True, include_system: bool = False) -> Task | None:
    canonical_title = canonicalize_task_title(title)
    recent = await get_recent_tasks(db, limit=80, include_system=include_system)
    for task in recent:
        if not include_closed and task.status not in _OPEN_STATUSES:
            continue
        if titles_look_similar(task.title or "", canonical_title):
            return task
    return None


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
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            extra = f"[{timestamp}] {note.strip()}"
            existing.notes = f"{existing.notes}\n{extra}" if existing.notes else extra
        if estimated_minutes and not existing.estimated_minutes:
            existing.estimated_minutes = estimated_minutes
        if category:
            existing.category = category
        if status in _OPEN_STATUSES:
            existing.completed_at = None
        elif status == "done":
            existing.completed_at = datetime.now(timezone.utc)
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
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        task.notes = f"[{timestamp}] {note.strip()}"
    if status == "done":
        task.completed_at = datetime.now(timezone.utc)
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


async def start_task_timer(task_id: str, db: AsyncSession) -> None:
    """Starts invisible timer when task enters in_progress."""
    now_iso = datetime.now(timezone.utc).isoformat()
    await set_setting(f"task_{task_id}_started_at", now_iso, db)
    await set_setting("active_task_id", str(task_id), db)
    await set_setting("active_task_started_at", now_iso, db)


async def stop_task_timer(task_id: str, db: AsyncSession) -> int | None:
    """Stops timer and returns elapsed minutes. Returns None if no timer was active."""
    started_at_str = await get_setting(f"task_{task_id}_started_at", db=db)
    if not started_at_str:
        return None
    try:
        started_at = datetime.fromisoformat(started_at_str)
        now = datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = int((now - started_at).total_seconds() / 60)
        await set_setting(f"task_{task_id}_started_at", "", db)
        active = await get_setting("active_task_id", db=db)
        if active == str(task_id):
            await set_setting("active_task_id", "", db)
            await set_setting("active_task_started_at", "", db)
        return elapsed if elapsed > 0 else None
    except (ValueError, TypeError):
        return None


async def get_active_task_elapsed_minutes(db: AsyncSession) -> tuple[str | None, int]:
    """Returns (task_id, elapsed_minutes) for active task. (None, 0) if none."""
    task_id = await get_setting("active_task_id", db=db)
    started_str = await get_setting("active_task_started_at", db=db)
    if not task_id or not started_str:
        return None, 0
    try:
        started = datetime.fromisoformat(started_str)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = int((datetime.now(timezone.utc) - started).total_seconds() / 60)
        return task_id, elapsed
    except (ValueError, TypeError):
        return None, 0


async def update_task_status(task: Task, new_status: str, db: AsyncSession, note: str | None = None, category: str | None = None) -> Task:
    old_status = task.status
    task.status = new_status
    if category:
        task.category = category
    if new_status == "done":
        task.completed_at = datetime.now(timezone.utc)
        elapsed = await stop_task_timer(str(task.id), db)
        if elapsed is not None:
            task.actual_minutes = elapsed
    elif new_status == "in_progress" and old_status != "in_progress":
        task.completed_at = None
        await start_task_timer(str(task.id), db)
    elif new_status in _OPEN_STATUSES:
        task.completed_at = None
    if note:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        extra = f"[{timestamp}] {note.strip()}"
        task.notes = f"{task.notes}\n{extra}" if task.notes else extra
    await db.commit()
    await db.refresh(task)
    logger.info("Task status updated: {} -> {} (actual_minutes={})", task.title, new_status, task.actual_minutes)
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
    canonical_fragment = canonicalize_task_title(title_fragment)
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
        .where(Task.title.ilike(f"%{canonical_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if not task:
        return None, ""

    task.status = "done"
    task.completed_at = datetime.now(timezone.utc)
    elapsed = await stop_task_timer(str(task.id), db)
    if elapsed is not None:
        task.actual_minutes = elapsed
    await db.commit()
    logger.info("Task done: {} (id={})", task.title, task.id)

    if task.origin == "jira" and task.origin_ref:
        try:
            from app.services.jira_client import transition_issue
            await transition_issue(task.origin_ref, "Done")
            logger.info("Jira issue {} transitioned to Done", task.origin_ref)
        except Exception as exc:
            logger.warning("Failed to transition Jira issue {}: {}", task.origin_ref, exc)

    from app.services.gamification_service import award_task_completion
    final_xp, xp_loot_msg = await award_task_completion(task, db)
    return task, xp_loot_msg


async def delegate_task(title_fragment: str, delegated_to: str, db: AsyncSession) -> Task | None:
    canonical_fragment = canonicalize_task_title(title_fragment)
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
        .where(Task.title.ilike(f"%{canonical_fragment}%"))
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
    canonical_fragment = canonicalize_task_title(title_fragment)
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(_OPEN_STATUSES))
        .where(Task.title.ilike(f"%{canonical_fragment}%"))
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task:
        task.status = "dropped"
        await db.commit()
        logger.info("Task dropped: {}", task.title)
    return task


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