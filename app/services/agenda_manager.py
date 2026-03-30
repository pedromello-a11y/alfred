import re
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock

_RANGE_RE = re.compile(
    r"(?i)(?P<start_h>\d{1,2})(?::(?P<start_m>\d{2}))?\s*h?\s*(?:a|às?|ate|até|-)\s*(?P<end_h>\d{1,2})(?::(?P<end_m>\d{2}))?\s*h?"
)
_SINGLE_RE = re.compile(r"(?i)(?:às?|as)\s*(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*h?")
_DURATION_RE = re.compile(r"(?i)(?P<hours>\d+)\s*h")
_EXPLICIT_DATE_RE = re.compile(r"(?<!\d)(?P<day>\d{1,2})/(?P<month>\d{1,2})(?:/(?P<year>\d{2,4}))?")


def looks_like_agenda_input(text: str) -> bool:
    lowered = (text or "").lower()
    has_time = bool(_RANGE_RE.search(text or "") or _SINGLE_RE.search(text or ""))
    has_agenda_signal = any(
        signal in lowered
        for signal in ("reuni", "marquei", "agenda", "almoc", "almoç", "intervalo", "descanso", "break")
    )
    return has_time and has_agenda_signal


def _reference_day(text: str) -> date:
    today = date.today()
    lowered = (text or "").lower()
    explicit = _EXPLICIT_DATE_RE.search(text or "")
    if explicit:
        day = int(explicit.group("day"))
        month = int(explicit.group("month"))
        year = explicit.group("year")
        year_val = int(year) if year else today.year
        if year_val < 100:
            year_val += 2000
        try:
            return date(year_val, month, day)
        except ValueError:
            return today
    if "amanhã" in lowered or "amanha" in lowered:
        return today + timedelta(days=1)
    return today


def _infer_block_type(text: str) -> str:
    lowered = (text or "").lower()
    if any(term in lowered for term in ("almoc", "almoç", "intervalo", "descanso", "break")):
        return "break"
    if "reuni" in lowered:
        return "meeting"
    if any(term in lowered for term in ("pessoal", "consulta", "médico", "medico")):
        return "personal"
    return "focus"


def _infer_title(text: str) -> str:
    lowered = (text or "").lower()
    cleaned = re.sub(r"\s+", " ", (text or "")).strip(" .:-")
    if "intervalo" in lowered and ("almoc" in lowered or "almoç" in lowered):
        return "Intervalo + Almoço"
    if "almoc" in lowered or "almoç" in lowered:
        return "Almoço"
    if any(term in lowered for term in ("intervalo", "descanso", "break")):
        return "Intervalo / Descanso"
    if "reuni" in lowered:
        if "barbara" in lowered or "bárbara" in lowered:
            return "Reunião com Bárbara"
        if "3k" in lowered:
            return "Reunião com 3K"
        if "cavazza" in lowered:
            return "Reunião com Cavazza"
        if "ato de abertura" in lowered:
            return "Reunião — Ato de Abertura"
        return "Reunião"
    if "referenc" in lowered:
        return "Buscar referências"
    if "spark" in lowered and "video" in lowered:
        return "Spark — edição de vídeo"
    return cleaned[:120] if cleaned else "Bloco de foco"


def extract_agenda_blocks(text: str) -> list[dict[str, Any]]:
    if not text:
        return []

    blocks: list[dict[str, Any]] = []
    ref_day = _reference_day(text)
    block_type = _infer_block_type(text)
    title = _infer_title(text)

    for match in _RANGE_RE.finditer(text):
        start_h = int(match.group("start_h"))
        start_m = int(match.group("start_m") or 0)
        end_h = int(match.group("end_h"))
        end_m = int(match.group("end_m") or 0)
        start_at = datetime(ref_day.year, ref_day.month, ref_day.day, start_h, start_m)
        end_at = datetime(ref_day.year, ref_day.month, ref_day.day, end_h, end_m)
        if end_at <= start_at:
            end_at = start_at + timedelta(hours=1)
        blocks.append({
            "title": title,
            "start_at": start_at,
            "end_at": end_at,
            "block_type": block_type,
        })

    if blocks:
        return blocks

    single = _SINGLE_RE.search(text)
    if not single:
        return []

    duration_match = _DURATION_RE.search(text)
    duration_hours = int(duration_match.group("hours")) if duration_match else 1
    start_h = int(single.group("h"))
    start_m = int(single.group("m") or 0)
    start_at = datetime(ref_day.year, ref_day.month, ref_day.day, start_h, start_m)
    end_at = start_at + timedelta(hours=duration_hours)
    blocks.append({
        "title": title,
        "start_at": start_at,
        "end_at": end_at,
        "block_type": block_type,
    })
    return blocks


async def upsert_agenda_block(
    title: str,
    start_at: datetime,
    end_at: datetime,
    block_type: str,
    source: str | None,
    db: AsyncSession,
    linked_task_id: UUID | None = None,
    notes: str | None = None,
) -> AgendaBlock:
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.title == title)
        .where(AgendaBlock.start_at == start_at)
        .where(AgendaBlock.end_at == end_at)
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.block_type = block_type
        existing.source = source or existing.source
        existing.notes = notes or existing.notes
        existing.linked_task_id = linked_task_id or existing.linked_task_id
        await db.commit()
        await db.refresh(existing)
        return existing

    block = AgendaBlock(
        title=title,
        start_at=start_at,
        end_at=end_at,
        block_type=block_type,
        source=source,
        linked_task_id=linked_task_id,
        notes=notes,
    )
    db.add(block)
    await db.commit()
    await db.refresh(block)
    return block


async def capture_agenda_from_text(
    text: str,
    db: AsyncSession,
    *,
    linked_task_id: UUID | None = None,
    source: str | None = "whatsapp",
) -> list[AgendaBlock]:
    blocks = extract_agenda_blocks(text)
    persisted: list[AgendaBlock] = []
    for block in blocks:
        persisted.append(
            await upsert_agenda_block(
                block["title"],
                block["start_at"],
                block["end_at"],
                block["block_type"],
                source,
                db,
                linked_task_id=linked_task_id,
                notes=text[:300],
            )
        )
    return persisted


async def get_current_agenda_block(db: AsyncSession) -> AgendaBlock | None:
    now = datetime.now()
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at <= now)
        .where(AgendaBlock.end_at > now)
        .where(AgendaBlock.status == "planned")
        .order_by(AgendaBlock.start_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
