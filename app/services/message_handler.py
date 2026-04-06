"""
Ponto de entrada para processamento de mensagens WhatsApp.

Assinatura pública (não muda):
    handle(raw_text, origin, db) -> tuple[InboundItem, str, str]

InboundItem também é exportado daqui para compatibilidade com callers externos
(runtime_router.py, task_manager.py).

Handlers individuais vivem em app/services/messaging/handlers/.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import agenda_manager, brain, task_manager

# ── Handlers extraídos ───────────────────────────────────────────────────────
from app.services.messaging.handlers.commands import (
    handle_day_off_accept as _handle_day_off_accept,
    handle_delegate as _handle_delegate,
    handle_drop as _handle_drop,
    handle_prestige_accept as _handle_prestige_accept,
    handle_ritual_choice as _handle_ritual_choice,
)
from app.services.messaging.handlers.context_updates import (
    handle_context_update as _handle_context_update,
    handle_explicit_done_update as _handle_explicit_done_update,
    looks_like_context_update as _looks_like_context_update,
    looks_like_explicit_done_update as _looks_like_explicit_done_update,
    looks_like_operational_status_update as _looks_like_operational_status_update,
)
from app.services.messaging.handlers.crisis import (
    handle_crisis_message as _handle_crisis_message,
    handle_unstuck_flow as _handle_unstuck_flow,
)
from app.services.messaging.handlers.dumps import handle_dump as _handle_dump
from app.services.messaging.handlers.queries import (
    handle_active_tasks as _handle_active_tasks,
    handle_context_query as _handle_context_query,
)
from app.services.messaging.handlers.scheduling import (
    handle_gcal_confirm as _handle_gcal_confirm,
    handle_schedule_intent as _handle_schedule_intent,
)
from app.services.messaging.handlers.utils import (
    agenda_blocks_inline as _agenda_blocks_inline,
    agenda_capture_response as _agenda_capture_response,
    build_context as _build_context,
    capture_context_note as _capture_context_note,
    maybe_grant_rest_xp as _maybe_grant_rest_xp,
)


# ── Dataclass público (importado por runtime_router e task_manager) ───────────

@dataclass
class InboundItem:
    item_type: str
    origin: str
    raw_text: str
    extracted_title: str
    deadline: Optional[date] = None
    priority_hint: Optional[str] = None
    category: Optional[str] = None
    needs_confirmation: bool = False
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


# ── Constantes / padrões (usados por handle() e _route()) ────────────────────

_ITEM_TYPE_MAP = {
    "new_task": "task",
    "update": "update",
    "question": "task",
    "command": "task",
    "chat": "idea",
}

_RESET_PATTERNS = re.compile(r"(?i)(zere os dados|reset sistema|limpar tudo|apagar todas as tarefas)")

_UNSTUCK_PATTERNS = re.compile(
    r"(?i)(tô travado|to travado|travei|tô bloqueado|to bloqueado|bloqueado|não consigo|nao consigo|"
    r"tá difícil|ta difícil|não tô conseguindo|nao to conseguindo)"
)
_DELEGATE_PATTERNS = re.compile(r"(?i)(não é comigo|nao é comigo|não é meu|isso não é pra mim|delegar|vou delegar)")
_DROP_PATTERNS = re.compile(r"(?i)(não importa mais|nao importa mais|cancelar tarefa|desistir|deixa pra lá|deixa pra la|não precisa mais|nao precisa mais)")
_DUMP_PREFIX = re.compile(r"(?i)^dump:\s*")
_CRISIS_PATTERNS = re.compile(r"(?i)(não dou conta|nao dou conta|tô mal|to mal|muita coisa|ansiedade|esgotado|esgotada|não aguento|nao aguento|tô esgotado|to esgotado|não consigo mais|nao consigo mais|burnout|queimado|desisto|não aguento mais|nao aguento mais)")
_CRISIS_RECOVERY_PATTERNS = re.compile(r"(?i)(melhorei|tô melhor|to melhor|me sinto melhor|voltei|pronto pra trabalhar|pode voltar ao normal|cancela modo crise|sai do modo crise)")
_PRESTIGE_ACCEPT = re.compile(r"(?i)^sim$")
_DAY_OFF_ACCEPT = re.compile(r"(?i)^respiro$")
_REST_ACCEPT = re.compile(r"(?i)^(ok|bora|pode ser|tá|ta|valeu|beleza)$")
_SCHEDULE_INTENT = re.compile(r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)")
_SCHEDULE_CONFIRM = re.compile(r"(?i)^(sim|cria|pode criar|confirma|ok cria)$")
_CONTEXT_QUERY = re.compile(r"(?i)^contexto\s+(.+)$")
_TECHNICAL_DETAIL = re.compile(r"(?i)(erro|error|bug|cliente|client|decisão|decisao|decidimos|aprovado|reprovado|feedback|reunião|reuniao|bloqueado|depende de|aguardando)")
_ACTIVE_TASKS_QUERY = re.compile(
    r"(?i)^(quais (são|sao) )?(minhas )?(tarefas|demandas|atividades) (ativas|em aberto|abertas)\??$|"
    r"^(me diga )?(minhas )?(demandas|atividades) (abertas|ativas)\??$|"
    r"^todas atividades que tem em aberto\??$|"
    r"^(o )?que (tenho|está|esta) (em aberto|aberto agora|ativo agora)\??$"
)
_SYSTEM_FEEDBACK_HINTS = re.compile(r"(?i)(era pra implementar o sistema|nao adicionar como demand|não adicionar como demand|seria importante voce ser|seria importante você ser)")
_REFERENCE_HINTS = re.compile(r"^(lembrar|salvar|guardar).*(filme|série|serie|video|vídeo|referencia|referência)\b|\bdump\b|n[aã]o e tarefa\b", re.IGNORECASE)
_RENAME_HINTS = re.compile(r"(?i)^separe assim\s+(.+)$")


# ── Fachada pública ───────────────────────────────────────────────────────────

async def handle(raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None) -> tuple[InboundItem, str, str]:
    raw_stripped = raw_text.strip()

    if db is not None:
        await task_manager.set_setting("ritual_answered", "true", db)

    # Bug 1.4: reset command
    if db is not None and _RESET_PATTERNS.search(raw_stripped):
        from sqlalchemy import select as _select
        from app.models import Task as _Task
        result = await db.execute(_select(_Task).where(_Task.status.in_(("pending", "in_progress"))))
        tasks_to_reset = list(result.scalars().all())
        for t in tasks_to_reset:
            t.status = "cancelled"
        await db.commit()
        item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="reset")
        return item, f"Pronto. {len(tasks_to_reset)} tarefas foram canceladas. Tela limpa.", "command"

    rename_match = _RENAME_HINTS.match(raw_stripped)
    if db is not None and rename_match:
        new_title = rename_match.group(1).strip()
        renamed = await task_manager.rename_most_recent_active_task(new_title, db)
        if renamed:
            item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title=renamed.title)
            return item, f"Ok. Vou tratar isso como: *{renamed.title}*.", "status_update"

    if db is not None and _SYSTEM_FEEDBACK_HINTS.search(raw_stripped):
        await _capture_context_note(raw_stripped, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="system_feedback")
        return item, "Entendi. Tratei isso como ajuste de comportamento do sistema, não como demanda operacional.", "context_update"

    if db is not None and _REFERENCE_HINTS.search(raw_stripped):
        response = await _handle_dump(f"dump: {raw_stripped}", origin, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="reference_dump")
        return item, response, "dump"

    if db is not None and _ACTIVE_TASKS_QUERY.match(raw_stripped):
        response = await _handle_active_tasks(db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="active_tasks")
        return item, response, "command"

    if _DUMP_PREFIX.match(raw_stripped):
        response = await _handle_dump(raw_stripped, origin, db)
        item = InboundItem(item_type="task", origin=origin, raw_text=raw_text, extracted_title=raw_stripped)
        return item, response, "dump"

    if db is not None and _looks_like_agenda_only_input(raw_stripped):
        blocks = await agenda_manager.capture_agenda_from_text(raw_stripped, db, source=origin)
        if blocks:
            item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="agenda_update")
            return item, _agenda_capture_response(blocks), "agenda_update"

    if db is not None and _looks_like_context_update(raw_stripped):
        response = await _handle_context_update(raw_stripped, db)
        item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="context_update")
        return item, response, "context_update"

    if db is not None and _looks_like_operational_status_update(raw_stripped):
        response = await _handle_context_update(raw_stripped, db)
        item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="status_update")
        return item, response, "status_update"

    if db is not None and _looks_like_explicit_done_update(raw_stripped):
        response = await _handle_explicit_done_update(raw_stripped, db)
        item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="status_update")
        return item, response, "status_update"

    if db is not None and raw_stripped in ("1", "2", "3"):
        awaiting = await task_manager.get_setting("awaiting_ritual_response", "false", db=db)
        if awaiting == "true":
            response = await _handle_ritual_choice(raw_stripped, db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="ritual_choice")
            return item, response, "command"

    if db is not None:
        crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
        if crisis_mode == "true":
            response = await _handle_crisis_message(raw_stripped, db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80])
            return item, response, "crisis"

    if db is not None and _PRESTIGE_ACCEPT.match(raw_stripped):
        prestige_offered = await task_manager.get_setting("prestige_offered", "false", db=db)
        if prestige_offered == "true":
            response = await _handle_prestige_accept(db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="prestige")
            return item, response, "command"

    if db is not None and _DAY_OFF_ACCEPT.match(raw_stripped):
        day_off_offered = await task_manager.get_setting("day_off_offered", "false", db=db)
        if day_off_offered == "true":
            response = await _handle_day_off_accept(db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="day_off")
            return item, response, "command"

    if db is not None and _REST_ACCEPT.match(raw_stripped):
        rest_granted = await task_manager.get_setting("rest_xp_granted_today", "false", db=db)
        if rest_granted != "true":
            await _maybe_grant_rest_xp(raw_stripped, db)

    if db is not None and _SCHEDULE_CONFIRM.match(raw_stripped):
        pending_event = await task_manager.get_setting("pending_gcal_event", db=db)
        if pending_event:
            response = await _handle_gcal_confirm(db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="gcal_event")
            return item, response, "command"

    if db is not None and _SCHEDULE_INTENT.search(raw_stripped):
        response = await _handle_schedule_intent(raw_stripped, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="schedule_intent")
        return item, response, "command"

    if db is not None:
        unstuck = await task_manager.get_setting("unstuck_mode", db=db)
        if unstuck == "true":
            response = await _handle_unstuck_flow(raw_text, db)
            item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80])
            return item, response, "unstuck"

    if _CRISIS_PATTERNS.search(raw_text):
        if db is not None:
            await task_manager.set_setting("crisis_mode", "true", db)
            await task_manager.set_setting("crisis_since", date.today().isoformat(), db)
        response = "Entendido. Vamos simplificar ao máximo.\nHoje só uma coisa. Sem pressão, sem backlog.\nFica à vontade pra me contar mais se quiser."
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="crisis")
        return item, response, "crisis"

    if _UNSTUCK_PATTERNS.search(raw_text):
        if db is not None:
            await task_manager.set_setting("unstuck_mode", "true", db)
            await task_manager.set_setting("unstuck_step", "1", db)
            await task_manager.set_setting("unstuck_used_today", "true", db)
        response = "Qual tarefa está travando? Me manda o nome ou número dela."
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="unstuck")
        return item, response, "unstuck"

    if _DELEGATE_PATTERNS.search(raw_text):
        response = await _handle_delegate(raw_text, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80])
        return item, response, "command"

    if _DROP_PATTERNS.search(raw_text):
        response = await _handle_drop(raw_text, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80])
        return item, response, "command"

    ctx_match = _CONTEXT_QUERY.match(raw_stripped)
    if ctx_match and db is not None:
        project_name = ctx_match.group(1).strip()
        response = await _handle_context_query(project_name, db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title=f"contexto:{project_name}")
        return item, response, "command"

    data = await brain.classify(raw_text, db=db)
    classification = data.get("classification", "chat")
    logger.info("Classification: {}", classification)

    deadline_raw = data.get("extracted_deadline")
    deadline: Optional[date] = None
    if deadline_raw:
        try:
            deadline = date.fromisoformat(str(deadline_raw))
        except ValueError:
            pass

    item = InboundItem(
        item_type=_ITEM_TYPE_MAP.get(classification, "idea"),
        origin=origin,
        raw_text=raw_text,
        extracted_title=data.get("extracted_title") or raw_text[:80],
        deadline=deadline,
        priority_hint=data.get("priority_hint"),
    )

    response_text = await _route(item, classification, db)
    return item, response_text, classification


# ── Orquestrador interno ──────────────────────────────────────────────────────

async def _route(item: InboundItem, classification: str, db: AsyncSession | None) -> str:
    if classification == "new_task":
        if db is not None:
            task = await task_manager.create(item, db)
            linked_blocks = await agenda_manager.capture_agenda_from_text(item.raw_text, db, linked_task_id=task.id, source=item.origin)
            boss_msg = "\n⚔️ Boss fight detectado! XP triplo se vencer. Quer enfrentar hoje?" if task.is_boss_fight else ""
            agenda_msg = f"\nAgenda vinculada: {_agenda_blocks_inline(linked_blocks)}" if linked_blocks else ""
            return f"Anotado: *{task.title}*. Prioridade: {item.priority_hint or 'normal'}.{agenda_msg}{boss_msg}"
        return f"Anotado: *{item.extracted_title}*."

    if classification == "update":
        if db is not None:
            return await _handle_context_update(item.raw_text, db)
        return "Entendi como atualização de contexto."

    if classification == "question":
        context = await _build_context(db)
        return await brain.answer_question(item.raw_text, context, db=db)

    if classification == "command":
        context = await _build_context(db)
        return await brain.execute_command(item.raw_text, context, db=db)

    if db is not None and _TECHNICAL_DETAIL.search(item.raw_text):
        await _capture_context_note(item.raw_text, db)
    return await brain.casual_response(item.raw_text, db=db)


# ── Predicado local (depende de agenda_manager, fica aqui) ───────────────────

def _looks_like_agenda_only_input(raw_text: str) -> bool:
    if not agenda_manager.looks_like_agenda_input(raw_text):
        return False
    lowered = raw_text.lower()
    blockers = (
        "demanda",
        "tarefa",
        "pra entregar",
        "deadline",
        "fila",
        "backlog",
        "preciso fazer",
        "tenho que fazer",
    )
    return not any(token in lowered for token in blockers)
