import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import agenda_manager, brain, dump_manager, jira_client, task_manager


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


_ITEM_TYPE_MAP = {
    "new_task": "task",
    "update": "update",
    "question": "task",
    "command": "task",
    "chat": "idea",
}

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
_CONTEXT_UPDATE_HINTS = re.compile(r"(?i)(resumo de demandas|demandas ativas|itens já resolvidos|itens ja resolvidos|detalhe[, ]|contexto de trabalho|status:|estimativa:|prioridade:|já foi feito|ja foi feito|já terminei|ja terminei|já entregou|ja entregou|já startei|ja startei|já comecei|ja comecei|combinei de)")
_EXPLICIT_DONE = re.compile(r"(?i)\b(terminei|finalizei|concluí|conclui|entreguei|foi entregue|foi aprovado|resolvido|resolvida|concluído|concluída|concluida)\b")
_NEGATED_DONE = re.compile(r"(?i)(ainda não terminei|ainda nao terminei|não terminei|nao terminei|não conclu[ií]|nao conclu[ií]|não entreguei|nao entreguei|ainda falta)")
_NEGATED_NOT_STARTED = re.compile(r"(?i)(ainda não comecei|ainda nao comecei|não comecei|nao comecei)")
_NEGATED_PENDING_TO_DONE = re.compile(r"(?i)(não está pendente|nao esta pendente|não está mais ativo|nao esta mais ativo|não está ativo|nao esta ativo)")
_NOTE_ONLY_HINTS = re.compile(r"(?i)(briefing|keyframes?|reuni[aã]o|3k|alinhar|alinhamento|assets prontos|assets chegaram|storyboard)")
_SYSTEM_HINTS = re.compile(r"(?i)(áudio|audio|bug do áudio|bug do audio|ajustes do sistema|sistema alfred|alfred continua quebrado)")
_SYSTEM_FEEDBACK_HINTS = re.compile(r"(?i)(era pra implementar o sistema|nao adicionar como demand|não adicionar como demand|seria importante voce ser|seria importante você ser)")
_REFERENCE_HINTS = re.compile(r"^(lembrar|salvar|guardar).*(filme|série|serie|video|vídeo|referencia|referência)\b|\bdump\b|n[aã]o e tarefa\b", re.IGNORECASE)
_RENAME_HINTS = re.compile(r"(?i)^separe assim\s+(.+)$")

_STATUS_PATTERNS = {
    "done": re.compile(r"(?i)(terminei|finalizei|concluí|conclui|entreguei|foi entregue|foi aprovado|resolvido|resolvida|concluído|concluída|concluida)"),
    "done_external": re.compile(r"(?i)(rig já fez|rig ja fez|rig já entregou|rig ja entregou|já entregou|ja entregou)"),
    "in_progress": re.compile(r"(?i)(em andamento|ativo agora|ativa agora|ativo|frente estratégica ativa|frente strategica ativa|startado|startei|comecei|iniciei|segue|continua|mandei briefing|briefing enviado|assets prontos|assets chegaram|está andando|esta andando|está rolando|esta rolando|rolando)"),
    "pending": re.compile(r"(?i)(pendente|em aberto|aberto|registrado|registrada|próximo da fila|proximo da fila|secundário|secundario|travado|pausou|voltou para pendente)"),
}

_SKIP_UPDATE_CHUNKS = re.compile(r"(?i)^(demandas ativas agora|outras demandas novas|itens já resolvidos|itens de radar|galaxy|spark|cast|detalhe)$")
_IGNORE_CONTEXT_CHUNKS = re.compile(r"(?i)^(esse é um resumo|esse e um resumo|demandas ativas agora|itens já resolvidos|itens ja resolvidos|outras demandas novas)$")
_FIELD_LINE_PREFIXES = re.compile(r"(?i)^(status|estimativa|estimativa que você me passou|estimativa que voce me passou|prioridade|briefing|cronograma|falta|função|funcao|checar|assets|ideia atual|preocupação principal|preocupacao principal)\s*:")
_GENERIC_TITLE_CANDIDATES = {
    "briefing", "keyframe", "keyframes", "reuniao", "reuniao com a 3k", "reunião", "reunião com a 3k",
    "entregue", "quase", "isso", "mas ainda nao", "mas ainda não", "audio", "áudio", "storyboard"
}
_TITLE_STOPWORDS = {
    "status", "ativa", "ativo", "agora", "demanda", "demandas", "aberto", "aberta", "pendente", "prioridade", "estimativa",
    "agendamento", "reuniao", "reunião", "feito", "feita", "terminei", "entregou", "entreguei", "ja", "já", "foi", "esta", "está",
    "com", "para", "sobre", "detalhe", "falta", "hoje", "rig", "mandei", "combinei", "andamento", "ativo", "resolvido",
    "resolvida", "concluido", "concluida", "concluído", "concluída", "enviado", "enviados", "assets", "prontos", "chegaram",
    "quase", "mas", "ainda", "nao", "não", "continua", "segue", "rolando", "voltou", "travado", "pausou", "startado", "startei"
}


async def handle(raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None) -> tuple[InboundItem, str, str]:
    raw_stripped = raw_text.strip()

    if db is not None:
        await task_manager.set_setting("ritual_answered", "true", db)

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


def _looks_like_context_update(raw_text: str) -> bool:
    return len(raw_text) >= 240 or bool(_CONTEXT_UPDATE_HINTS.search(raw_text))


def _looks_like_operational_status_update(raw_text: str) -> bool:
    if len(raw_text) > 220:
        return False
    if _ACTIVE_TASKS_QUERY.match(raw_text):
        return False
    status = _detect_status(raw_text)
    if not status:
        return False
    title = _extract_title_candidate(raw_text)
    if not title:
        return False
    if _is_note_only_candidate(title):
        return False
    normalized = task_manager.normalize_task_title(title)
    if normalized.startswith("esse e um resumo"):
        return False
    return True


def _looks_like_explicit_done_update(raw_text: str) -> bool:
    if _NEGATED_DONE.search(raw_text) or _NEGATED_NOT_STARTED.search(raw_text):
        return False
    return bool(_EXPLICIT_DONE.search(raw_text)) and len(raw_text) < 220


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


async def _build_context(db: AsyncSession | None) -> str:
    if db is None:
        return "(sem contexto disponível)"
    tasks = await task_manager.get_active_tasks(db)
    if tasks:
        lines = [f"- {t.title} (status {t.status}, prioridade {t.priority or '-'}, prazo {t.deadline or 'sem prazo'})" for t in tasks[:10]]
        return "Tarefas ativas:\n" + "\n".join(lines)
    jira_lines = await _build_jira_active_lines(db)
    if jira_lines:
        return "Demandas ativas do Jira/cache:\n" + "\n".join(jira_lines)
    return "Nenhuma tarefa ativa."


async def _handle_context_update(raw_text: str, db: AsyncSession) -> str:
    updates = await _extract_status_updates(raw_text, db)
    agenda_blocks = await agenda_manager.capture_agenda_from_text(raw_text, db)
    if not updates and not agenda_blocks:
        await _capture_context_note(raw_text, db)
        return "Entendi como atualização de contexto. Registrei isso sem marcar nenhuma tarefa como concluída.\nSe quiser, me pede depois: *minhas tarefas ativas*."
    if not updates and agenda_blocks:
        return _agenda_capture_response(agenda_blocks)

    applied_lines: list[str] = []
    unclear_lines: list[str] = []

    for upd in updates:
        task = upd.get("task")
        title = upd.get("title")
        status = upd["status"]
        note = upd.get("note")
        estimated_minutes = upd.get("estimated_minutes")
        category = upd.get("category", "work")

        if not task and title:
            mapped_status = _map_status_to_task_status(status)
            task = await task_manager.upsert_task_from_context(
                title,
                db,
                status=mapped_status,
                category=category,
                note=note,
                estimated_minutes=estimated_minutes,
            )
            if category != "system":
                applied_lines.append(f"- {task.title} → {_status_label(mapped_status)}")
            continue

        if not task:
            unclear_lines.append(f"- não consegui ligar com segurança: {upd['source'][:90]}")
            continue

        mapped_status = _map_status_to_task_status(status)
        updated_task = await task_manager.update_task_status(task, mapped_status, db, note=note, category=category)
        if category != "system":
            applied_lines.append(f"- {updated_task.title} → {_status_label(mapped_status)}")

    if not applied_lines and unclear_lines:
        return "Entendi como atualização de contexto, mas não apliquei nada com segurança:\n" + "\n".join(unclear_lines[:5])

    response = ["Atualizei seu estado atual assim:"]
    response.extend(applied_lines[:8])
    if agenda_blocks:
        response.append("\nAgenda registrada:")
        response.extend([f"- {_format_agenda_block(block)}" for block in agenda_blocks[:5]])
    if unclear_lines:
        response.append("\nPontos que deixei sem aplicar automaticamente:")
        response.extend(unclear_lines[:4])
    response.append("\nAgora, quando você pedir *minhas tarefas ativas*, eu vou considerar esse estado novo.")
    return "\n".join(response)


async def _handle_explicit_done_update(raw_text: str, db: AsyncSession) -> str:
    updates = await _extract_status_updates(raw_text, db)
    if not updates:
        return "Entendi como atualização, mas não consegui ligar isso com segurança a uma tarefa ativa."

    lines = []
    for upd in updates:
        task = upd.get("task")
        title = upd.get("title")
        status = upd["status"]
        note = upd.get("note")
        estimated_minutes = upd.get("estimated_minutes")
        category = upd.get("category", "work")

        if status == "done":
            if task:
                done_task, _ = await task_manager.mark_done(task.title, db)
                if done_task and category != "system":
                    lines.append(f"- {done_task.title} → concluída")
                    continue
            if title:
                created = await task_manager.upsert_task_from_context(title, db, status="done", category=category, note=note, estimated_minutes=estimated_minutes)
                if category != "system":
                    lines.append(f"- {created.title} → concluída")
                continue
        else:
            mapped_status = _map_status_to_task_status(status)
            if task:
                updated_task = await task_manager.update_task_status(task, mapped_status, db, note=note, category=category)
                if category != "system":
                    lines.append(f"- {updated_task.title} → {_status_label(mapped_status)}")
                continue
            if title:
                created = await task_manager.upsert_task_from_context(title, db, status=mapped_status, category=category, note=note, estimated_minutes=estimated_minutes)
                if category != "system":
                    lines.append(f"- {created.title} → {_status_label(mapped_status)}")
                continue

    if not lines:
        return "Entendi a intenção, mas não consegui aplicar nada com segurança."
    return "Atualização aplicada:\n" + "\n".join(lines)


async def _handle_active_tasks(db: AsyncSession) -> str:
    tasks = list(await task_manager.get_active_tasks(db))
    recent_done = list(await task_manager.get_recently_done(db))

    if not tasks:
        jira_lines = await _build_jira_active_lines(db)
        if jira_lines:
            response = ["Ativas agora (Jira/cache):"]
            response.extend(jira_lines[:5])
            if recent_done:
                response.append("\nResolvidas por último:")
                response.extend([f"- {t.title}" for t in recent_done[:3]])
            response.append("\nSe você me mandar um resumo de status, eu materializo isso na sua lista ativa.")
            return "\n".join(response)
        return "Você não tem tarefas ativas agora."

    top_now = tasks[:3]
    lines = ["Ativas agora:"]
    for i, task in enumerate(top_now, 1):
        extra = []
        if task.estimated_minutes:
            extra.append(f"~{task.estimated_minutes}min")
        if task.priority:
            extra.append(f"p{task.priority}")
        if task.deadline:
            extra.append(f"prazo {task.deadline.strftime('%d/%m')}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{i}. {task.title} — {_status_label(task.status)}{suffix}")

    if len(tasks) > 3:
        lines.append("\nEm acompanhamento:")
        for task in tasks[3:7]:
            lines.append(f"- {task.title} — {_status_label(task.status)}")

    if recent_done:
        lines.append("\nResolvidas por último:")
        for task in recent_done[:3]:
            lines.append(f"- {task.title}")

    lines.append("\nQual dessas está na sua mão agora?")
    return "\n".join(lines)


async def _build_jira_active_lines(db: AsyncSession) -> list[str]:
    cached = await jira_client.get_cached_issues(db)
    lines = []
    for issue in cached[:5]:
        title = issue.summary if hasattr(issue, "summary") else issue.get("summary")
        key = issue.jira_key if hasattr(issue, "jira_key") else issue.get("key")
        status = issue.status if hasattr(issue, "status") else issue.get("status")
        if title:
            prefix = f"[{key}] " if key else ""
            suffix = f" — {status}" if status else ""
            lines.append(f"- {prefix}{title}{suffix}")
    return lines


async def _extract_status_updates(raw_text: str, db: AsyncSession) -> list[dict]:
    chunks = _split_update_chunks(raw_text)
    active_tasks = list(await task_manager.get_active_tasks(db, include_system=True))
    recent_tasks = list(await task_manager.get_recent_tasks(db, limit=80, include_system=True))
    all_tasks = active_tasks + [t for t in recent_tasks if t not in active_tasks]
    updates: list[dict] = []
    seen_keys: set[str] = set()
    anchor_title: str | None = None
    anchor_task = None
    anchor_category = "work"
    anchor_status = "pending"

    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped or _SKIP_UPDATE_CHUNKS.match(stripped) or _IGNORE_CONTEXT_CHUNKS.match(stripped):
            continue

        category = _infer_category(stripped)
        status = _detect_status(stripped)
        title = _extract_title_candidate(stripped)
        note = _extract_note(stripped)
        estimated_minutes = _extract_estimated_minutes(stripped)
        task = _match_task_for_chunk(stripped, title, all_tasks, include_system=(category == "system"))
        is_field_line = _looks_like_field_line(stripped)
        is_section_header = _looks_like_section_header(stripped)

        if is_section_header:
            continue

        if title and not status and not is_field_line:
            if category != "system":
                anchor_title = title
                anchor_task = task
                anchor_category = category
                anchor_status = "pending"
            continue

        if is_field_line and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            field_note = _field_line_note(stripped)
            note = f"{note} | {field_note}" if note and field_note and field_note not in note else (field_note or note)
            status = status or anchor_status
            category = anchor_category

        if _NOTE_ONLY_HINTS.search(stripped) and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            if status is None:
                status = anchor_status or "in_progress"
            category = anchor_category

        if _is_note_only_candidate(title) and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            if status is None:
                status = anchor_status or "in_progress"
            category = anchor_category

        if status is None:
            continue

        if task is None and title is None and (anchor_task or anchor_title):
            task = anchor_task
            title = anchor_title
            note = f"{note} | {stripped}" if note and stripped not in note else (note or stripped)
            category = anchor_category

        if task or title:
            if category != "system":
                anchor_task = task
                anchor_title = task.title if task else task_manager.canonicalize_task_title(title)
                anchor_category = category
                anchor_status = status

        dedupe = f"{(task.title if task else title) or stripped}:{status}:{category}:{note or ''}"
        if dedupe in seen_keys:
            continue
        seen_keys.add(dedupe)
        updates.append({
            "task": task,
            "title": task_manager.canonicalize_task_title(title) if title else None,
            "status": status,
            "note": note,
            "estimated_minutes": estimated_minutes,
            "category": category,
            "source": stripped,
        })
    return updates


def _split_update_chunks(raw_text: str) -> list[str]:
    normalized = raw_text.replace("\t", " ")
    raw_parts = re.split(r"\n+|•|\*|;", normalized)
    parts: list[str] = []
    for part in raw_parts:
        cleaned = part.strip(" -–—:\n")
        if cleaned:
            parts.append(cleaned)
    return parts


def _looks_like_field_line(chunk: str) -> bool:
    return bool(_FIELD_LINE_PREFIXES.match(chunk))


def _looks_like_section_header(chunk: str) -> bool:
    normalized = task_manager.normalize_task_title(chunk)
    return normalized in {
        "demandas ativas agora",
        "outras demandas novas",
        "itens ja resolvidos",
        "itens resolvidos",
        "galaxy fire abertura",
        "spark",
        "galaxy",
        "cast",
    }


def _field_line_note(chunk: str) -> str | None:
    match = _FIELD_LINE_PREFIXES.match(chunk)
    if not match:
        return None
    return chunk.strip()


def _detect_status(chunk: str) -> str | None:
    normalized = task_manager.normalize_task_title(chunk)
    if _NEGATED_NOT_STARTED.search(chunk):
        return "pending"
    if _NEGATED_DONE.search(chunk):
        return "in_progress"
    if _NEGATED_PENDING_TO_DONE.search(chunk) and ("entreg" in normalized or "resolve" in normalized or "conclu" in normalized):
        return "done"
    if "nao esta mais ativo" in normalized or "não está mais ativo" in chunk.lower():
        return "done"
    if _STATUS_PATTERNS["done_external"].search(chunk):
        return "done"
    if _STATUS_PATTERNS["in_progress"].search(chunk):
        return "in_progress"
    if _STATUS_PATTERNS["done"].search(chunk):
        return "done"
    if _STATUS_PATTERNS["pending"].search(chunk):
        return "pending"
    return None


def _extract_note(chunk: str) -> str | None:
    lowered = chunk.lower()
    notes = []
    if "3k" in lowered and ("11h" in lowered or "11 h" in lowered):
        notes.append("Reunião com a 3K marcada para segunda às 11h")
    if "storyboard" in lowered:
        notes.append(chunk)
    if "briefing" in lowered and ("keyframe" in lowered or "keyframes" in lowered):
        notes.append("Briefing e keyframes enviados")
    elif "briefing" in lowered or "keyframe" in lowered or "keyframes" in lowered:
        notes.append(chunk)
    if "rig" in lowered:
        notes.append(chunk)
    return " | ".join(dict.fromkeys(notes)) if notes else None


def _extract_estimated_minutes(chunk: str) -> int | None:
    match = re.search(r"~?\s*(\d+)h(?:\s*(\d+))?", chunk, re.IGNORECASE)
    if match:
        hours = int(match.group(1))
        extra = int(match.group(2)) if match.group(2) else 0
        return hours * 60 + extra
    match_min = re.search(r"~?\s*(\d+)\s*min", chunk, re.IGNORECASE)
    if match_min:
        return int(match_min.group(1))
    return None


def _extract_title_candidate(chunk: str) -> str | None:
    text = re.sub(r"(?i)^detalhe:\s*", "", chunk).strip()
    normalized_text = task_manager.normalize_task_title(text)
    if normalized_text.startswith("esse e um resumo"):
        return None

    for sep in (" — ", " - ", " – ", ":"):
        if sep in text:
            left = text.split(sep, 1)[0].strip()
            if len(left) >= 3:
                return task_manager.canonicalize_task_title(left)

    original = text
    cleaned = task_manager.normalize_task_title(text)
    phrase_noise = [
        "ainda nao terminei", "nao terminei", "nao conclui", "nao entreguei", "ainda falta", "nao comecei",
        "esta em andamento", "esta andando", "esta rolando", "estao rolando", "segue em andamento", "segue", "continua",
        "terminei", "finalizei", "conclui", "entreguei", "foi entregue", "foi aprovado", "resolvido", "concluido",
        "pendente", "ativo", "em andamento", "startado", "startei", "comecei", "iniciei", "ja foi startado",
        "ja foi", "voltou para pendente", "assets prontos", "assets chegaram", "enviado", "enviados"
    ]
    for phrase in phrase_noise:
        cleaned = cleaned.replace(phrase, " ")
    tokens = [w for w in cleaned.split() if w and w not in _TITLE_STOPWORDS]
    if not tokens:
        return None
    candidate = " ".join(tokens[:8]).strip()
    if candidate in _GENERIC_TITLE_CANDIDATES:
        return candidate

    original_tokens = re.findall(r"[A-Za-zÀ-ÿ0-9|/]+", original)
    filtered_original = [w for w in original_tokens if task_manager.normalize_task_title(w) not in _TITLE_STOPWORDS]
    if filtered_original:
        rebuilt = " ".join(filtered_original[:8]).strip()
        if rebuilt:
            return task_manager.canonicalize_task_title(rebuilt)
    return task_manager.canonicalize_task_title(candidate) if candidate else None


def _match_task_for_chunk(chunk: str, title_candidate: str | None, tasks: list, include_system: bool = False):
    if not tasks:
        return None
    lowered_chunk = task_manager.normalize_task_title(chunk)
    canonical_candidate = task_manager.canonicalize_task_title(title_candidate) if title_candidate else None
    best_task = None
    best_score = 0.0

    for task in tasks:
        if not include_system and (task.category in ("backlog", "system") or task_manager.is_system_task_title(task.title or "")):
            continue
        title = task.title or ""
        normalized_title = task_manager.normalize_task_title(task_manager.canonicalize_task_title(title))
        score = 0.0
        if canonical_candidate and task_manager.titles_look_similar(title, canonical_candidate):
            score += 24
        ratio = SequenceMatcher(None, lowered_chunk[:160], normalized_title).ratio()
        score += ratio * 8
        keywords = [w for w in lowered_chunk.split() if w and w not in _TITLE_STOPWORDS]
        overlap = sum(1 for kw in keywords if kw in normalized_title)
        score += overlap * 3
        if "motion avisos" in lowered_chunk and "motion avisos" in normalized_title:
            score += 14
        if "avisos do spark" in lowered_chunk and "motion avisos" in normalized_title:
            score += 14
        if "countdown" in lowered_chunk and "countdown" in normalized_title:
            score += 8
        if "screensaver" in lowered_chunk and "screensaver" in normalized_title:
            score += 8
        if ("video de abertura" in lowered_chunk or "abertura fire" in lowered_chunk or "projeto da 3k" in lowered_chunk or "3k" in lowered_chunk) and "video de abertura" in normalized_title:
            score += 16
        if score > best_score:
            best_score = score
            best_task = task

    if best_score < 8:
        return None
    return best_task


def _infer_category(chunk: str) -> str:
    if _SYSTEM_HINTS.search(chunk):
        return "system"
    return "work"


def _is_note_only_candidate(title: str | None) -> bool:
    if not title:
        return False
    normalized = task_manager.normalize_task_title(title)
    generic = {task_manager.normalize_task_title(x) for x in _GENERIC_TITLE_CANDIDATES}
    return normalized in generic


def _map_status_to_task_status(status: str) -> str:
    if status == "done":
        return "done"
    if status == "in_progress":
        return "in_progress"
    return "pending"


def _status_label(status: str) -> str:
    return {
        "done": "concluída",
        "in_progress": "em andamento",
        "pending": "pendente",
        "delegated": "delegada",
        "dropped": "removida",
    }.get(status, status)


def _format_agenda_block(block) -> str:
    return f"{block.title} — {block.start_at.strftime('%d/%m %H:%M')}→{block.end_at.strftime('%H:%M')}"


def _agenda_blocks_inline(blocks) -> str:
    return "; ".join(_format_agenda_block(block) for block in blocks[:3])


def _agenda_capture_response(blocks) -> str:
    lines = ["Agenda registrada:"]
    lines.extend([f"- {_format_agenda_block(block)}" for block in blocks[:5]])
    return "\n".join(lines)


async def _handle_unstuck_flow(raw_text: str, db: AsyncSession) -> str:
    step = int(await task_manager.get_setting("unstuck_step", "1", db=db) or "1")
    if step == 1:
        await task_manager.set_setting("unstuck_task", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "2", db)
        return "Qual o menor pedaço que dá pra fazer em 5 minutos?"
    if step == 2:
        await task_manager.set_setting("unstuck_micro", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "3", db)
        return "Faz só isso agora. Me avisa quando terminar. 🎯"
    if step == 3:
        await task_manager.set_setting("unstuck_step", "4", db)
        return "Show! ✅ Quer fazer mais 5 minutos ou parar aqui?"
    await task_manager.set_setting("unstuck_mode", "false", db)
    await task_manager.set_setting("unstuck_step", "1", db)
    if any(w in raw_text.lower() for w in ("mais", "continuar", "seguir", "sim")):
        return "Bora! Qual o próximo micro-passo?"
    return "Ótimo trabalho. Quando quiser continuar, é só falar."


async def _handle_crisis_message(raw_text: str, db: AsyncSession) -> str:
    if _CRISIS_RECOVERY_PATTERNS.search(raw_text):
        await task_manager.set_setting("crisis_mode", "false", db)
        await task_manager.set_setting("crisis_since", "", db)
        return "Fico feliz! 🙌 Voltamos ao ritmo normal. Quando quiser ver suas tarefas, é só pedir."
    prompt = "Pedro está passando por um período difícil (modo crise ativo). Responda de forma empática e gentil, sem mencionar tarefas, backlog ou produtividade. Mensagem dele: " + raw_text
    return await brain.casual_response(prompt, db=db)


async def _handle_prestige_accept(db: AsyncSession) -> str:
    from sqlalchemy import select
    from app.models import PlayerStat

    attrs = ["craft", "strategy", "life", "willpower", "knowledge"]
    result = await db.execute(select(PlayerStat).where(PlayerStat.attribute.in_(attrs)))
    stats = result.scalars().all()
    prestige_num = (stats[0].prestige + 1) if stats else 1
    for stat in stats:
        stat.prestige = prestige_num
        stat.xp = 0
        stat.level = 1
    await task_manager.set_setting("prestige_offered", "false", db)
    await db.commit()
    multiplier = 1 + (prestige_num * 0.1)
    return f"🌟 *PRESTIGE {prestige_num} ATIVADO!*\nTodos os atributos resetados para nível 1.\nMultiplicador permanente: {multiplier:.1f}x XP.\nNova jornada começa agora. 💪"


async def _handle_delegate(raw_text: str, db: AsyncSession | None) -> str:
    if db is None:
        return "Anotado! Pra quem vai delegar?"
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return "Sem tarefas ativas pra delegar."
    task_to_delegate = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_delegate = t
            break
    await task_manager.delegate_task(task_to_delegate.title, "a definir", db)
    return f"*{task_to_delegate.title}* marcada como delegada. Pra quem vai?"


async def _handle_dump(raw_stripped: str, origin: str, db: AsyncSession | None) -> str:
    dump_text = _DUMP_PREFIX.sub("", raw_stripped).strip()
    if not dump_text:
        return "Dump vazio — manda o que quer registrar depois de 'dump:'"
    if db is None:
        return "Registrado em dumps."

    item = await dump_manager.create_dump_item(raw_stripped, origin, db)
    current_block = await agenda_manager.get_current_agenda_block(db)
    if current_block and current_block.block_type == "break":
        focus_line = f"Segue no seu descanso: *{current_block.title}*."
    elif current_block:
        focus_line = f"Depois volta pra *{current_block.title}*."
    else:
        pending = await task_manager.get_active_tasks(db)
        focus_line = f"Volta pra *{pending[0].title}*." if pending else "Isso não vai se perder."

    return f"Registrado em dumps como *{item.rewritten_title}* ({item.category or 'desconhecido'}).\n{focus_line}"


async def _handle_ritual_choice(choice: str, db: AsyncSession) -> str:
    from sqlalchemy import select as _select
    from app.models import DailyPlan, Task as _Task

    await task_manager.set_setting("awaiting_ritual_response", "false", db)
    today = date.today()
    result = await db.execute(_select(DailyPlan).where(DailyPlan.plan_date == today))
    plan = result.scalar_one_or_none()
    if plan and plan.tasks_planned and "ids" in plan.tasks_planned:
        idx = int(choice) - 1
        task_ids = plan.tasks_planned["ids"]
        if 0 <= idx < len(task_ids):
            task_id = task_ids[idx]
            await task_manager.set_setting("daily_victory_task_id", task_id, db)
            t_result = await db.execute(_select(_Task).where(_Task.id == task_id))
            task = t_result.scalar_one_or_none()
            if task:
                return f"Ótimo! Foco em *{task.title}*. Bora! 🎯"
    return f"Opção {choice} registrada. Bora começar! 🎯"


async def _maybe_grant_rest_xp(raw_text: str, db: AsyncSession) -> None:
    from sqlalchemy import select as _select
    from app.models import Message as _Message

    pause_keywords = ("pausa", "descanso", "coffee break", "descanse", "respira", "break")
    last_out = await db.execute(_select(_Message).where(_Message.direction == "outbound").order_by(_Message.created_at.desc()).limit(1))
    last_outbound = last_out.scalar_one_or_none()
    if not last_outbound or not any(kw in last_outbound.content.lower() for kw in pause_keywords):
        return
    stat = await task_manager.grant_xp("recovery", 10, db)
    await task_manager.set_setting("rest_xp_granted_today", "true", db)
    logger.info("Rest XP granted mid-conversation: +10 recovery (nível {})", stat.level)


async def _handle_day_off_accept(db: AsyncSession) -> str:
    await task_manager.set_setting("day_off_tomorrow", "true", db)
    await task_manager.set_setting("day_off_offered", "false", db)
    return "Combinado! 🌿 Amanhã é dia de respiro.\nSem plano, sem cobranças. E na volta: 1.5x XP em tudo. 💪"


async def _handle_schedule_intent(raw_text: str, db: AsyncSession) -> str:
    import json
    from datetime import datetime, timedelta, timezone
    from app.services import gcal_client

    duration_min = 60
    dur_match = re.search(r"(\d+)\s*(h(?:ora)?s?|min(?:utos?)?)", raw_text, re.IGNORECASE)
    if dur_match:
        val = int(dur_match.group(1))
        unit = dur_match.group(2).lower()
        duration_min = val * 60 if unit.startswith("h") else val
    task_name_match = re.sub(r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)\s*", "", raw_text).strip()
    task_name = task_name_match[:100] if task_name_match else "Foco"
    events = await gcal_client.get_today_events()
    now = datetime.now(timezone.utc)
    start_candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_work = now.replace(hour=21, minute=0, second=0, microsecond=0)
    occupied = []
    for e in events:
        try:
            from dateutil.parser import parse as _parse
            es = _parse(e["start"]).astimezone(timezone.utc)
            ee = _parse(e["end"]).astimezone(timezone.utc)
            occupied.append((es, ee))
        except Exception:
            pass
    slot_start = start_candidate
    slot_found = False
    for _ in range(8):
        slot_end = slot_start + timedelta(minutes=duration_min)
        conflict = any(not (slot_end <= os or slot_start >= oe) for os, oe in occupied)
        if not conflict and slot_end <= end_work:
            slot_found = True
            break
        slot_start += timedelta(hours=1)
    if not slot_found:
        return "Não achei slot livre hoje. Quer agendar pra amanhã de manhã?"
    slot_brt_start = slot_start - timedelta(hours=3)
    slot_brt_end = (slot_start + timedelta(minutes=duration_min)) - timedelta(hours=3)
    time_str = f"{slot_brt_start.strftime('%H:%M')} → {slot_brt_end.strftime('%H:%M')}"
    proposal = {"title": task_name, "start": slot_start.isoformat(), "end": (slot_start + timedelta(minutes=duration_min)).isoformat()}
    await task_manager.set_setting("pending_gcal_event", json.dumps(proposal), db)
    return f"Sugiro *{time_str}* pra '{task_name}' ({duration_min}min).\nCrio na agenda? Responde *sim* pra confirmar."


async def _handle_gcal_confirm(db: AsyncSession) -> str:
    import json
    from datetime import datetime
    from app.services import gcal_client

    raw = await task_manager.get_setting("pending_gcal_event", db=db)
    if not raw:
        return "Não encontrei nenhum evento pendente de confirmação."
    proposal = json.loads(raw)
    await task_manager.set_setting("pending_gcal_event", "", db)
    start_dt = datetime.fromisoformat(proposal["start"])
    end_dt = datetime.fromisoformat(proposal["end"])
    result = await gcal_client.create_event(proposal["title"], start_dt, end_dt)
    if result.get("status") == "ok":
        return f"✅ Evento *{proposal['title']}* criado na agenda!"
    return f"Não consegui criar o evento: {result.get('error', 'erro desconhecido')}"


async def _append_task_note(task, raw_text: str, db: AsyncSession) -> None:
    snippet = raw_text[:300].strip()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    addition = f"[{now_str}] {snippet}"
    task.notes = f"{task.notes}\n{addition}" if task.notes else addition
    await db.commit()
    logger.debug("Context note appended to task {}", task.id)


async def _capture_context_note(raw_text: str, db: AsyncSession) -> None:
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return
    raw_lower = raw_text.lower()
    matched_task = None
    for t in tasks[:10]:
        title_words = [w for w in (t.title or "").lower().split() if len(w) > 3]
        if title_words and any(w in raw_lower for w in title_words):
            matched_task = t
            break
    if matched_task:
        await _append_task_note(matched_task, raw_text, db)


async def _handle_context_query(project_name: str, db: AsyncSession) -> str:
    from sqlalchemy import select as _select
    from app.models import Memory as _Memory, Task as _Task

    tasks_result = await db.execute(_select(_Task).where(_Task.title.ilike(f"%{project_name}%")).order_by(_Task.created_at.desc()).limit(10))
    tasks = tasks_result.scalars().all()
    mem_result = await db.execute(_select(_Memory).where(_Memory.content.ilike(f"%{project_name}%")).where(_Memory.superseded == False).order_by(_Memory.period_start.desc()).limit(3))
    memories = mem_result.scalars().all()
    if not tasks and not memories:
        return f"Não encontrei contexto para '{project_name}'. Tenta com outra palavra-chave."
    lines = [f"Contexto do projeto/tarefa '{project_name}':"]
    for t in tasks:
        status_icon = "✅" if t.status == "done" else "⏳"
        lines.append(f"{status_icon} {t.title} ({t.status})")
        if t.notes:
            lines.append(f"   📝 {t.notes[:200]}")
    if memories:
        lines.append("\nNo histórico:")
        for m in memories[:2]:
            for sentence in m.content.split(". "):
                if project_name.lower() in sentence.lower():
                    lines.append(f"  - {sentence.strip()[:150]}")
                    break
    return "\n".join(lines)


async def _handle_drop(raw_text: str, db: AsyncSession | None) -> str:
    if db is None:
        return "Qual tarefa quer remover?"
    tasks = await task_manager.get_active_tasks(db)
    if not tasks:
        return "Sem tarefas ativas."
    task_to_drop = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_drop = t
            break
    await task_manager.drop_task(task_to_drop.title, db)
    return f"*{task_to_drop.title}* removida da lista. Sem penalidade. ✂️"