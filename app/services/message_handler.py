import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import brain, task_manager


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

_DELEGATE_PATTERNS = re.compile(
    r"(?i)(não é comigo|nao é comigo|não é meu|isso não é pra mim|delegar|vou delegar)"
)

_DROP_PATTERNS = re.compile(
    r"(?i)(não importa mais|nao importa mais|cancelar tarefa|desistir|deixa pra lá|deixa pra la|"
    r"não precisa mais|nao precisa mais)"
)

_DUMP_PREFIX = re.compile(r"(?i)^dump:\s*")

_CRISIS_PATTERNS = re.compile(
    r"(?i)(não dou conta|nao dou conta|tô mal|to mal|muita coisa|ansiedade|"
    r"esgotado|esgotada|não aguento|nao aguento|tô esgotado|to esgotado|"
    r"não consigo mais|nao consigo mais|burnout|queimado|desisto|não aguento mais|nao aguento mais)"
)

_CRISIS_RECOVERY_PATTERNS = re.compile(
    r"(?i)(melhorei|tô melhor|to melhor|me sinto melhor|voltei|pronto pra trabalhar|"
    r"pode voltar ao normal|cancela modo crise|sai do modo crise)"
)

_PRESTIGE_ACCEPT = re.compile(r"(?i)^sim$")
_DAY_OFF_ACCEPT = re.compile(r"(?i)^respiro$")
_REST_ACCEPT = re.compile(r"(?i)^(ok|bora|pode ser|tá|ta|valeu|beleza)$")
_SCHEDULE_INTENT = re.compile(
    r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)"
)
_SCHEDULE_CONFIRM = re.compile(r"(?i)^(sim|cria|pode criar|confirma|ok cria)$")
_CONTEXT_QUERY = re.compile(r"(?i)^contexto\s+(.+)$")
_TECHNICAL_DETAIL = re.compile(
    r"(?i)(erro|error|bug|cliente|client|decisão|decisao|decidimos|aprovado|"
    r"reprovado|feedback|reunião|reuniao|bloqueado|depende de|aguardando)"
)
_ACTIVE_TASKS_QUERY = re.compile(
    r"(?i)^(quais (são|sao) )?(minhas )?(tarefas|demandas) (ativas|em aberto)\??$|^(o )?que (tenho|está|esta) (em aberto|aberto agora|ativo agora)\??$"
)
_CONTEXT_UPDATE_HINTS = re.compile(
    r"(?i)(resumo de demandas|demandas ativas|itens já resolvidos|itens ja resolvidos|"
    r"detalhe[, ]|contexto de trabalho|status:|estimativa:|prioridade:|já foi feito|ja foi feito|"
    r"já terminei|ja terminei|já entregou|ja entregou|já startei|ja startei|já comecei|ja comecei|combinei de)"
)
_EXPLICIT_DONE = re.compile(
    r"(?i)(terminei|finalizei|concluí|conclui|entreguei|já foi|ja foi|aprovado|resolvido|resolvida|feito|feita)"
)

_STATUS_PATTERNS = {
    "done": re.compile(r"(?i)(já terminei|ja terminei|terminei|finalizei|concluí|conclui|entreguei|resolvido|resolvida|aprovado|aprovada|já foi feito|ja foi feito|já foi|ja foi|ok / resolvido|ok\/resolvido|concluído|concluida|concluída)"),
    "done_external": re.compile(r"(?i)(rig já fez|rig ja fez|rig já entregou|rig ja entregou|já entregou|ja entregou)"),
    "in_progress": re.compile(r"(?i)(em andamento|ativo agora|ativa agora|frente estratégica ativa|frente estrategica ativa|startei|startei|comecei|iniciei|mandei briefing|briefing enviado|assets prontos|assets chegaram)"),
    "pending": re.compile(r"(?i)(pendente|em aberto|aberto|registrado|registrada|próximo da fila|proximo da fila|secundário|secundario)"),
}

_SKIP_UPDATE_CHUNKS = re.compile(r"(?i)^(demandas ativas agora|outras demandas novas|itens já resolvidos|itens de radar|galaxy|spark|cast|detalhe)$")


async def handle(
    raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None
) -> tuple[InboundItem, str, str]:
    raw_stripped = raw_text.strip()

    if db is not None:
        await task_manager.set_setting("ritual_answered", "true", db)

    if db is not None and _ACTIVE_TASKS_QUERY.match(raw_stripped):
        response = await _handle_active_tasks(db)
        item = InboundItem(item_type="idea", origin=origin, raw_text=raw_text, extracted_title="active_tasks")
        return item, response, "command"

    if db is not None and _looks_like_context_update(raw_stripped):
        response = await _handle_context_update(raw_stripped, db)
        item = InboundItem(item_type="update", origin=origin, raw_text=raw_text, extracted_title="context_update")
        return item, response, "context_update"

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

    if _DUMP_PREFIX.match(raw_stripped):
        response = await _handle_dump(raw_stripped, origin, db)
        item = InboundItem(item_type="task", origin=origin, raw_text=raw_text, extracted_title=raw_stripped)
        return item, response, "dump"

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
        response = (
            "Entendido. Vamos simplificar ao máximo.\n"
            "Hoje só uma coisa. Sem pressão, sem backlog.\n"
            "Fica à vontade pra me contar mais se quiser."
        )
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
    if len(raw_text) >= 240:
        return True
    if _CONTEXT_UPDATE_HINTS.search(raw_text):
        return True
    return False


def _looks_like_explicit_done_update(raw_text: str) -> bool:
    return bool(_EXPLICIT_DONE.search(raw_text)) and len(raw_text) < 220


async def _route(item: InboundItem, classification: str, db: AsyncSession | None) -> str:
    if classification == "new_task":
        if db is not None:
            task = await task_manager.create(item, db)
            boss_msg = ""
            if task.is_boss_fight:
                boss_msg = "\n⚔️ Boss fight detectado! XP triplo se vencer. Quer enfrentar hoje?"
            return f"Anotado: *{task.title}*. Prioridade: {item.priority_hint or 'normal'}.{boss_msg}"
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
    if not tasks:
        return "Nenhuma tarefa ativa."
    lines = [f"- {t.title} (status {t.status}, prioridade {t.priority or '-'}, prazo {t.deadline or 'sem prazo'})" for t in tasks[:10]]
    return "Tarefas ativas:\n" + "\n".join(lines)


async def _handle_context_update(raw_text: str, db: AsyncSession) -> str:
    updates = await _extract_status_updates(raw_text, db)
    if not updates:
        await _capture_context_note(raw_text, db)
        return (
            "Entendi como atualização de contexto. Registrei isso sem marcar nenhuma tarefa como concluída.\n"
            "Se quiser, me pede depois: *minhas tarefas ativas*."
        )

    applied_lines: list[str] = []
    unclear_lines: list[str] = []

    for upd in updates:
        task = upd.get("task")
        if not task:
            unclear_lines.append(f"- não consegui ligar com segurança: {upd['source'][:90]}")
            continue

        status = upd["status"]
        note = upd.get("note")

        if status == "done":
            done_task, _ = await task_manager.mark_done(task.title, db)
            if done_task:
                applied_lines.append(f"- {done_task.title} → concluída")
            else:
                unclear_lines.append(f"- não consegui concluir: {task.title}")
            continue

        mapped_status = "in_progress" if status == "in_progress" else "pending"
        updated_task = await task_manager.update_task_status(task, mapped_status, db, note=note)
        label = "em andamento" if mapped_status == "in_progress" else "pendente"
        applied_lines.append(f"- {updated_task.title} → {label}")

    if not applied_lines and unclear_lines:
        return "Entendi como atualização de contexto, mas não apliquei nada com segurança:\n" + "\n".join(unclear_lines[:5])

    response = ["Atualizei seu estado atual assim:"]
    response.extend(applied_lines[:8])
    if unclear_lines:
        response.append("\nPontos que deixei sem aplicar automaticamente:")
        response.extend(unclear_lines[:4])
    response.append("\nAgora, quando você pedir *minhas tarefas ativas*, eu vou considerar esse estado novo.")
    return "\n".join(response)


async def _handle_explicit_done_update(raw_text: str, db: AsyncSession) -> str:
    updates = await _extract_status_updates(raw_text, db)
    done_updates = [u for u in updates if u.get("task") and u["status"] == "done"]
    non_done = [u for u in updates if u.get("task") and u["status"] != "done"]

    if not done_updates and not non_done:
        return "Entendi como atualização, mas não consegui ligar isso com segurança a uma tarefa ativa."

    lines = []
    for upd in done_updates:
        task = upd["task"]
        done_task, _ = await task_manager.mark_done(task.title, db)
        if done_task:
            lines.append(f"- {done_task.title} → concluída")

    for upd in non_done:
        task = upd["task"]
        mapped_status = "in_progress" if upd["status"] == "in_progress" else "pending"
        label = "em andamento" if mapped_status == "in_progress" else "pendente"
        updated_task = await task_manager.update_task_status(task, mapped_status, db, note=upd.get("note"))
        lines.append(f"- {updated_task.title} → {label}")

    if not lines:
        return "Entendi a intenção, mas não consegui aplicar nada com segurança."

    return "Atualização aplicada:\n" + "\n".join(lines)


async def _handle_active_tasks(db: AsyncSession) -> str:
    tasks = list(await task_manager.get_active_tasks(db))
    recent_done = list(await task_manager.get_recently_done(db, limit=3))
    if not tasks:
        return "Você não tem tarefas ativas agora."

    top_now = tasks[:3]
    lines = ["Ativas agora:"]
    for i, task in enumerate(top_now, 1):
        status_label = "em andamento" if task.status == "in_progress" else "pendente"
        extra = []
        if task.estimated_minutes:
            extra.append(f"~{task.estimated_minutes}min")
        if task.priority:
            extra.append(f"p{task.priority}")
        if task.deadline:
            extra.append(f"prazo {task.deadline.strftime('%d/%m')}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{i}. {task.title} — {status_label}{suffix}")

    if len(tasks) > 3:
        lines.append("\nEm acompanhamento:")
        for task in tasks[3:7]:
            status_label = "em andamento" if task.status == "in_progress" else "pendente"
            lines.append(f"- {task.title} — {status_label}")

    if recent_done:
        lines.append("\nResolvidas por último:")
        for task in recent_done[:3]:
            lines.append(f"- {task.title}")

    lines.append("\nQual dessas está na sua mão agora?")
    return "\n".join(lines)


async def _extract_status_updates(raw_text: str, db: AsyncSession) -> list[dict]:
    chunks = _split_update_chunks(raw_text)
    tasks = list(await task_manager.get_active_tasks(db))
    updates: list[dict] = []
    seen_ids: set[str] = set()

    for chunk in chunks:
        if not chunk or _SKIP_UPDATE_CHUNKS.match(chunk.strip()):
            continue
        status = _detect_status(chunk)
        if not status:
            continue
        task = _match_task_for_chunk(chunk, tasks)
        note = _extract_note(chunk)
        key = f"{getattr(task, 'id', None)}:{status}:{chunk[:40]}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        updates.append({"task": task, "status": status, "note": note, "source": chunk})
    return updates


def _split_update_chunks(raw_text: str) -> list[str]:
    normalized = raw_text.replace("\t", " ")
    raw_parts = re.split(r"\n+|•|;", normalized)
    parts: list[str] = []
    for part in raw_parts:
        subparts = re.split(r",\s+(?=[a-zA-ZÀ-ÿ0-9])", part)
        for sub in subparts:
            cleaned = sub.strip(" -–—:\n")
            if cleaned:
                parts.append(cleaned)
    return parts


def _detect_status(chunk: str) -> str | None:
    if _STATUS_PATTERNS["done_external"].search(chunk):
        return "done"
    for status in ("done", "in_progress", "pending"):
        if _STATUS_PATTERNS[status].search(chunk):
            return status
    return None


def _extract_note(chunk: str) -> str | None:
    lowered = chunk.lower()
    if "3k" in lowered and ("11h" in lowered or "11 h" in lowered):
        return "Reunião com a 3K marcada para segunda às 11h"
    if "mandei briefing" in lowered or "briefing" in lowered and "keyframe" in lowered:
        return "Briefing e keyframes enviados"
    if "rig" in lowered:
        return chunk
    return None


def _chunk_keywords(chunk: str) -> list[str]:
    words = re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", chunk.lower())
    stopwords = {
        "status", "ativa", "ativo", "agora", "demanda", "demandas", "aberto", "aberta",
        "pendente", "prioridade", "estimativa", "agendamento", "reunião", "reuniao",
        "feito", "feita", "terminei", "entregou", "entreguei", "já", "ja", "foi", "está", "esta",
        "com", "para", "sobre", "detalhe", "falta", "hoje", "agora", "rig", "mandei", "combinei",
    }
    return [w for w in words if w not in stopwords]


def _match_task_for_chunk(chunk: str, tasks: list) -> object | None:
    keywords = _chunk_keywords(chunk)
    if not keywords:
        return None

    best_task = None
    best_score = 0.0
    lowered_chunk = chunk.lower()

    for task in tasks:
        title = (task.title or "").lower()
        overlap = sum(1 for kw in keywords if kw in title)
        ratio = SequenceMatcher(None, lowered_chunk[:160], title).ratio()
        score = overlap * 10 + ratio * 5
        if "fire" in lowered_chunk and "abertura" in lowered_chunk and ("fire" in title or "abertura" in title):
            score += 12
        if "countdown" in lowered_chunk and "countdown" in title:
            score += 12
        if "screensaver" in lowered_chunk and "screensaver" in title:
            score += 12
        if "motion" in lowered_chunk and "avisos" in lowered_chunk and "motion" in title:
            score += 12
        if score > best_score:
            best_score = score
            best_task = task

    if best_score < 8:
        return None
    return best_task


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

    prompt = (
        "Pedro está passando por um período difícil (modo crise ativo). "
        "Responda de forma empática e gentil, sem mencionar tarefas, backlog ou produtividade. "
        f"Mensagem dele: {raw_text}"
    )
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
    return (
        f"🌟 *PRESTIGE {prestige_num} ATIVADO!*\n"
        f"Todos os atributos resetados para nível 1.\n"
        f"Multiplicador permanente: {multiplier:.1f}x XP.\n"
        f"Nova jornada começa agora. 💪"
    )


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

    if db is not None:
        from app.models import Task
        task = Task(title=dump_text[:500], origin=origin, status="dump", category="backlog")
        db.add(task)
        await db.commit()

    foco = "sua tarefa atual"
    if db is not None:
        victory_id = await task_manager.get_setting("daily_victory_task_id", db=db)
        if victory_id:
            from sqlalchemy import select as _select
            from app.models import Task as _Task
            result = await db.execute(_select(_Task).where(_Task.id == victory_id))
            victory = result.scalar_one_or_none()
            if victory:
                foco = victory.title
        else:
            pending = await task_manager.get_active_tasks(db)
            if pending:
                foco = pending[0].title

    return f"Registrado. Isso não vai se perder.\nVolta pra *{foco}*."


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
    last_out = await db.execute(
        _select(_Message).where(_Message.direction == "outbound").order_by(_Message.created_at.desc()).limit(1)
    )
    last_outbound = last_out.scalar_one_or_none()
    if not last_outbound:
        return
    if not any(kw in last_outbound.content.lower() for kw in pause_keywords):
        return

    stat = await task_manager.grant_xp("recovery", 10, db)
    await task_manager.set_setting("rest_xp_granted_today", "true", db)
    logger.info("Rest XP granted mid-conversation: +10 recovery (nível {})", stat.level)


async def _handle_day_off_accept(db: AsyncSession) -> str:
    await task_manager.set_setting("day_off_tomorrow", "true", db)
    await task_manager.set_setting("day_off_offered", "false", db)
    return (
        "Combinado! 🌿 Amanhã é dia de respiro.\n"
        "Sem plano, sem cobranças. E na volta: 1.5x XP em tudo. 💪"
    )


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

    task_name_match = re.sub(
        r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)\s*",
        "", raw_text,
    ).strip()
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
    if not matched_task:
        return
    await _append_task_note(matched_task, raw_text, db)


async def _handle_context_query(project_name: str, db: AsyncSession) -> str:
    from sqlalchemy import select as _select
    from app.models import Memory as _Memory, Task as _Task

    tasks_result = await db.execute(
        _select(_Task).where(_Task.title.ilike(f"%{project_name}%")).order_by(_Task.created_at.desc()).limit(10)
    )
    tasks = tasks_result.scalars().all()

    mem_result = await db.execute(
        _select(_Memory)
        .where(_Memory.content.ilike(f"%{project_name}%"))
        .where(_Memory.superseded == False)
        .order_by(_Memory.period_start.desc())
        .limit(3)
    )
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
