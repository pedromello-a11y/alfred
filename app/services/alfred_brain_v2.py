from app.services import brain, runtime_router
from app.services.focus_snapshot import build_focus_snapshot


def _build_operational_tail(snapshot: dict, classification: str) -> str:
    if classification == "question":
        return ""

    current_block = snapshot.get("currentBlock") or {}
    suggestion = snapshot.get("suggestion") or {}

    if current_block.get("title"):
        return f"\n\nAgora: *{current_block.get('title')}* até {current_block.get('end', '')}."

    if suggestion.get("title"):
        title = suggestion.get("title")
        reason = suggestion.get("reason") or "próximo foco"
        return f"\n\nPróximo foco: *{title}* ({reason})."

    return ""


async def process_message(text, db, origin="whatsapp"):
    raw_text = (text or "").strip()
    if not raw_text:
        return "", "ignored"

    _item, response_text, classification = await runtime_router.handle(
        raw_text,
        origin=origin,
        db=db,
    )

    base = (response_text or "Entendi.").strip()
    cls = (classification or "unknown").strip()

    try:
        snapshot = await build_focus_snapshot(db)
        tail = _build_operational_tail(snapshot, cls)
    except Exception:
        tail = ""

    return (base + tail).strip(), cls


async def generate_text(
    prompt,
    db=None,
    *,
    model=None,
    max_tokens=300,
    temperature=0.3,
    call_type="general",
    include_history=False,
):
    raw_prompt = (prompt or "").strip()
    if not raw_prompt:
        return ""

    return await brain._call(
        raw_prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        call_type=call_type,
        db=db,
        include_history=include_history,
    )
