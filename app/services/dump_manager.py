import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DumpItem

_DUMP_PREFIX = re.compile(r"(?i)^dump:\s*")


@dataclass
class DumpClassification:
    rewritten_title: str
    summary: str
    category: str
    subcategory: str | None
    confidence: float
    status: str


def cleanup_dump_text(raw_text: str) -> str:
    text = _DUMP_PREFIX.sub("", raw_text or "").strip()
    return re.sub(r"\s+", " ", text)


def _looks_like_bare_title(text: str) -> bool:
    tokens = [t for t in re.findall(r"[A-Za-zÀ-ÿ0-9]+", text) if t]
    if not tokens or len(tokens) > 5:
        return False
    lowered = text.lower()
    blocked = ("preciso", "tenho", "comprar", "fazer", "arrumar", "consertar", "ligar", "mandar")
    return not any(word in lowered for word in blocked)


def classify_dump(raw_text: str) -> DumpClassification:
    cleaned = cleanup_dump_text(raw_text)
    lowered = cleaned.lower()

    if lowered.startswith("comprar "):
        item = cleaned[8:].strip(" .")
        return DumpClassification(
            rewritten_title=f"Compra: {item.title()}",
            summary=f"Item pessoal para comprar depois: {item}.",
            category="compras",
            subcategory="pessoal",
            confidence=0.96,
            status="categorized",
        )

    if any(term in lowered for term in ("filme", "cinema", "assistir")):
        title = re.sub(r"(?i)^(filme|assistir)\s*", "", cleaned).strip(" :.-")
        title = title or cleaned
        return DumpClassification(
            rewritten_title=f"Filme: {title.title()}",
            summary=f"Referência de filme para consultar depois: {title}.",
            category="filmes",
            subcategory="entretenimento",
            confidence=0.9,
            status="categorized",
        )

    if any(term in lowered for term in ("serie", "série")):
        title = re.sub(r"(?i)^s[eé]rie\s*", "", cleaned).strip(" :.-")
        title = title or cleaned
        return DumpClassification(
            rewritten_title=f"Série: {title.title()}",
            summary=f"Referência de série para consultar depois: {title}.",
            category="series",
            subcategory="entretenimento",
            confidence=0.9,
            status="categorized",
        )

    if any(term in lowered for term in ("livro", "ler")):
        title = re.sub(r"(?i)^(livro|ler)\s*", "", cleaned).strip(" :.-")
        title = title or cleaned
        return DumpClassification(
            rewritten_title=f"Livro: {title.title()}",
            summary=f"Referência de leitura para consultar depois: {title}.",
            category="livros",
            subcategory="referencias",
            confidence=0.85,
            status="categorized",
        )

    if any(term in lowered for term in ("referencia", "referência", "inspiração", "inspiracao")):
        return DumpClassification(
            rewritten_title=f"Referência: {cleaned[:120]}",
            summary=f"Referência rápida capturada para revisitar depois.",
            category="referencias",
            subcategory="criativo",
            confidence=0.75,
            status="categorized",
        )

    if _looks_like_bare_title(cleaned):
        return DumpClassification(
            rewritten_title=cleaned.title(),
            summary=f"Item salvo para revisar e categorizar depois: {cleaned}.",
            category="desconhecido",
            subcategory=None,
            confidence=0.35,
            status="unknown",
        )

    return DumpClassification(
        rewritten_title=f"Dump: {cleaned[:120]}",
        summary=f"Informação capturada para revisar depois.",
        category="desconhecido",
        subcategory=None,
        confidence=0.25,
        status="unknown",
    )


async def create_dump_item(raw_text: str, origin: str | None, db: AsyncSession, source_task_id: UUID | None = None) -> DumpItem:
    classified = classify_dump(raw_text)
    item = DumpItem(
        raw_text=cleanup_dump_text(raw_text),
        rewritten_title=classified.rewritten_title,
        summary=classified.summary,
        category=classified.category,
        subcategory=classified.subcategory,
        confidence=classified.confidence,
        status=classified.status,
        source=origin,
        source_task_id=source_task_id,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def list_dump_items(db: AsyncSession, limit: int = 50) -> list[DumpItem]:
    result = await db.execute(
        select(DumpItem)
        .order_by(DumpItem.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
