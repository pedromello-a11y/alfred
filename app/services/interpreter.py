import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgendaBlock
from app.services import brain, task_manager

_INTERPRET_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_SUPPORTED_INTENTS = {
    "new_task",
    "dump",
    "agenda_add",
    "task_update",
    "correction",
    "system_feedback",
    "context_note",
    "question",
    "chat",
    "unknown",
}
_SUPPORTED_BLOCK_TYPES = {"focus", "meeting", "break", "personal", "admin"}


async def _build_interpreter_context(db: AsyncSession) -> str:
    now = datetime.now()
    today = date.today()

    tasks = list(await task_manager.get_active_tasks(db))[:8]
    task_lines = []
    for task in tasks:
        deadline = task.deadline.strftime("%Y-%m-%d") if task.deadline else "sem prazo"
        task_lines.append(f"- {task.title} | status={task.status} | prazo={deadline}")
    if not task_lines:
        task_lines.append("- nenhuma task ativa")

    start_day = datetime.combine(today, time.min)
    end_day = start_day + timedelta(days=1)
    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= start_day)
        .where(AgendaBlock.start_at < end_day)
        .order_by(AgendaBlock.start_at.asc())
    )
    agenda = result.scalars().all()
    agenda_lines = []
    for block in agenda[:8]:
        agenda_lines.append(
            f"- {block.title} | {block.start_at.strftime('%H:%M')}->{block.end_at.strftime('%H:%M')} | tipo={block.block_type}"
        )
    if not agenda_lines:
        agenda_lines.append("- nenhum bloco hoje")

    history = []
    try:
        history = await brain.get_recent_messages(db, limit=5, max_hours=8)
    except Exception:
        history = []
    history_lines = []
    for item in history[-5:]:
        history_lines.append(f"- {item['role']}: {item['content'][:180]}")
    if not history_lines:
        history_lines.append("- sem histórico recente")

    last_action_type = await task_manager.get_setting("last_action_type", db=db)
    last_action_id = await task_manager.get_setting("last_action_id", db=db)

    return (
        f"Hora atual BRT: {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"Última ação gravada: tipo={last_action_type or 'nenhuma'} id={last_action_id or 'nenhum'}\n\n"
        f"Tasks ativas:\n" + "\n".join(task_lines) + "\n\n"
        f"Agenda de hoje:\n" + "\n".join(agenda_lines) + "\n\n"
        f"Histórico recente:\n" + "\n".join(history_lines)
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    match = _INTERPRET_JSON_RE.search(raw.strip())
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _sanitize(data: dict[str, Any], raw_text: str) -> dict[str, Any] | None:
    intent = str(data.get("intent") or "unknown").strip()
    if intent not in _SUPPORTED_INTENTS:
        intent = "unknown"

    try:
        confidence = float(data.get("confidence", 0) or 0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    blocks: list[dict[str, Any]] = []
    for block in data.get("time_blocks") or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("block_type") or "focus")
        if block_type not in _SUPPORTED_BLOCK_TYPES:
            block_type = "focus"
        blocks.append(
            {
                "title": (block.get("title") or raw_text[:80] or "Bloco").strip(),
                "start_at": block.get("start_at"),
                "end_at": block.get("end_at"),
                "block_type": block_type,
            }
        )

    return {
        "intent": intent,
        "confidence": confidence,
        "task_title": (data.get("task_title") or "").strip(),
        "project": (data.get("project") or "").strip(),
        "deadline_iso": data.get("deadline_iso"),
        "task_status": (data.get("task_status") or "").strip(),
        "category": (data.get("category") or "work").strip() or "work",
        "reference_title": (data.get("reference_title") or "").strip(),
        "correction_new_type": (data.get("correction_new_type") or "").strip(),
        "time_blocks": blocks,
        "note": (data.get("note") or "").strip(),
        "raw_text": raw_text,
    }


async def interpret_message(raw_text: str, db: AsyncSession) -> dict[str, Any] | None:
    ctx = await _build_interpreter_context(db)
    prompt = f"""
Você é o interpretador central do Alfred.
Sua tarefa é classificar UMA mensagem do Pedro em UMA intenção principal e extrair entidades.

Regras obrigatórias:
- Responda APENAS JSON válido.
- Nunca use markdown.
- Escolha uma intenção principal entre:
  new_task, dump, agenda_add, task_update, correction, system_feedback, context_note, question, chat, unknown
- Se a mensagem for correção do que acabou de ser anotado, use intent=correction.
- Se a mensagem só quer guardar algo para acessar depois, use intent=dump.
- Se a mensagem estiver marcando horário/bloco/reunião/descanso, use intent=agenda_add.
- Se a mensagem estiver informando avanço/conclusão/estado de algo existente, use intent=task_update.
- Se a mensagem estiver criando uma demanda nova, use intent=new_task.
- Se a mensagem estiver falando do comportamento do próprio Alfred/sistema/bot, use intent=system_feedback.
- Se a mensagem for só contexto, observação ou nota que não deve virar task nem agenda, use intent=context_note.
- Só preencha time_blocks quando a intenção principal for agenda_add.
- Em correction, preencha correction_new_type com dump, task, agenda_block ou note.
- Use deadline_iso e time_blocks.start_at/end_at em ISO 8601 completo no timezone America/Sao_Paulo.
- Se não tiver confiança, use intent=unknown e confidence baixo.

Contexto atual:
{ctx}

Mensagem do Pedro:
{raw_text}

Schema:
{{
  "intent": "new_task|dump|agenda_add|task_update|correction|system_feedback|context_note|question|chat|unknown",
  "confidence": 0.0,
  "task_title": "",
  "project": "",
  "deadline_iso": null,
  "task_status": "pending|in_progress|done|",
  "category": "work|personal|system|",
  "reference_title": "",
  "correction_new_type": "dump|task|agenda_block|note|",
  "time_blocks": [
    {{"title": "", "start_at": "", "end_at": "", "block_type": "focus|meeting|break|personal|admin"}}
  ],
  "note": ""
}}
""".strip()

    raw = await brain._call(
        prompt,
        model=brain.settings.model_smart,
        max_tokens=350,
        temperature=0.1,
        call_type="interpret",
        db=db,
        include_history=False,
    )
    data = _extract_json(raw)
    if not data:
        return None
    return _sanitize(data, raw_text)
