from app.services import brain, runtime_router


async def process_message(text, db, origin="whatsapp"):
    raw_text = (text or "").strip()
    if not raw_text:
        return "", "ignored"

    _item, response_text, classification = await runtime_router.handle(
        raw_text,
        origin=origin,
        db=db,
    )
    return (response_text or "Entendi.").strip(), (classification or "unknown").strip()


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
