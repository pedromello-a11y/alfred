import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import brain, task_manager


# ---------------------------------------------------------------------------
# InboundItem — formato padrão de entrada para todos os canais
# ---------------------------------------------------------------------------

@dataclass
class InboundItem:
    item_type: str              # 'task', 'reminder', 'idea', 'event', 'update'
    origin: str                 # 'whatsapp', 'jira', 'gcal', 'gmail'
    raw_text: str               # texto original
    extracted_title: str        # título extraído
    deadline: Optional[date] = None
    priority_hint: Optional[str] = None  # 'high', 'medium', 'low'
    category: Optional[str] = None       # 'work', 'personal'
    needs_confirmation: bool = False
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


_ITEM_TYPE_MAP = {
    "new_task": "task",
    "update":   "update",
    "question": "task",
    "command":  "task",
    "chat":     "idea",
}

# ---------------------------------------------------------------------------
# Padrões ADHD — destravamento, delegate, drop
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Padrões de crise (anti-burnout)
# ---------------------------------------------------------------------------

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

# F7 — Intenção de agendar bloco na agenda
_SCHEDULE_INTENT = re.compile(
    r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)"
)
_SCHEDULE_CONFIRM = re.compile(r"(?i)^(sim|cria|pode criar|confirma|ok cria)$")

# F15 — Captura de contexto operacional
_CONTEXT_QUERY = re.compile(r"(?i)^contexto\s+(.+)$")
_TECHNICAL_DETAIL = re.compile(
    r"(?i)(erro|error|bug|cliente|client|decisão|decisao|decidimos|aprovado|"
    r"reprovado|feedback|reunião|reuniao|bloqueado|depende de|aguardando)"
)


# ---------------------------------------------------------------------------
# handle — classifica via brain, normaliza para InboundItem, roteia
# Retorna (item, response_text, classification)
# ---------------------------------------------------------------------------

async def handle(
    raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None
) -> tuple["InboundItem", str, str]:
    """Classifica via Claude Haiku, roteia e retorna (item, response, classification)."""

    raw_stripped = raw_text.strip()

    # Marcar ritual de início como respondido (qualquer mensagem após briefing)
    if db is not None:
        await task_manager.set_setting("ritual_answered", "true", db)

    # --- Ritual de início: Pedro respondeu 1/2/3 → salvar vitória do dia ---
    if db is not None and raw_stripped in ("1", "2", "3"):
        awaiting = await task_manager.get_setting("awaiting_ritual_response", "false", db=db)
        if awaiting == "true":
            response = await _handle_ritual_choice(raw_stripped, db)
            item = InboundItem(
                item_type="idea", origin=origin,
                raw_text=raw_text, extracted_title="ritual_choice",
            )
            return item, response, "command"

    # --- Dump: registrar sem chamar Claude API ---
    if _DUMP_PREFIX.match(raw_stripped):
        response = await _handle_dump(raw_stripped, origin, db)
        item = InboundItem(
            item_type="task", origin=origin,
            raw_text=raw_text, extracted_title=raw_stripped,
        )
        return item, response, "dump"

    # --- Modo crise ativo: resposta suave, sem cobrança ---
    if db is not None:
        crisis_mode = await task_manager.get_setting("crisis_mode", "false", db=db)
        if crisis_mode == "true":
            response = await _handle_crisis_message(raw_stripped, db)
            item = InboundItem(
                item_type="idea", origin=origin,
                raw_text=raw_text, extracted_title=raw_text[:80],
            )
            return item, response, "crisis"

    # --- Aceite de prestige ---
    if db is not None and _PRESTIGE_ACCEPT.match(raw_stripped):
        prestige_offered = await task_manager.get_setting("prestige_offered", "false", db=db)
        if prestige_offered == "true":
            response = await _handle_prestige_accept(db)
            item = InboundItem(
                item_type="idea", origin=origin,
                raw_text=raw_text, extracted_title="prestige",
            )
            return item, response, "command"

    # --- Aceite de dia de respiro ---
    if db is not None and _DAY_OFF_ACCEPT.match(raw_stripped):
        day_off_offered = await task_manager.get_setting("day_off_offered", "false", db=db)
        if day_off_offered == "true":
            response = await _handle_day_off_accept(db)
            item = InboundItem(
                item_type="idea", origin=origin,
                raw_text=raw_text, extracted_title="day_off",
            )
            return item, response, "command"

    # --- Aceite de pausa sugerida → Rest XP (G8) ---
    if db is not None and _REST_ACCEPT.match(raw_stripped):
        rest_granted = await task_manager.get_setting("rest_xp_granted_today", "false", db=db)
        if rest_granted != "true":
            await _maybe_grant_rest_xp(raw_stripped, db)

    # --- F7: confirmar criação de evento no GCal ---
    if db is not None and _SCHEDULE_CONFIRM.match(raw_stripped):
        pending_event = await task_manager.get_setting("pending_gcal_event", db=db)
        if pending_event:
            response = await _handle_gcal_confirm(db)
            item = InboundItem(
                item_type="idea", origin=origin, raw_text=raw_text, extracted_title="gcal_event"
            )
            return item, response, "command"

    # --- F7: detectar intenção de agendar bloco ---
    if db is not None and _SCHEDULE_INTENT.search(raw_stripped):
        response = await _handle_schedule_intent(raw_stripped, db)
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title="schedule_intent"
        )
        return item, response, "command"

    # --- Verificar se está em modo unstuck ---
    if db is not None:
        unstuck = await task_manager.get_setting("unstuck_mode", db=db)
        if unstuck == "true":
            response = await _handle_unstuck_flow(raw_text, db)
            item = InboundItem(
                item_type="idea", origin=origin,
                raw_text=raw_text, extracted_title=raw_text[:80],
            )
            return item, response, "unstuck"

    # --- Detectar palavras de crise ---
    if _CRISIS_PATTERNS.search(raw_text):
        if db is not None:
            await task_manager.set_setting("crisis_mode", "true", db)
            await task_manager.set_setting("crisis_since", date.today().isoformat(), db)
        response = (
            "Entendido. Vamos simplificar ao máximo.\n"
            "Hoje só uma coisa. Sem pressão, sem backlog.\n"
            "Fica à vontade pra me contar mais se quiser."
        )
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title="crisis"
        )
        return item, response, "crisis"

    # --- Detectar padrões ADHD antes de classificar ---
    if _UNSTUCK_PATTERNS.search(raw_text):
        if db is not None:
            await task_manager.set_setting("unstuck_mode", "true", db)
            await task_manager.set_setting("unstuck_step", "1", db)
            await task_manager.set_setting("unstuck_used_today", "true", db)
        response = "Qual tarefa está travando? Me manda o nome ou número dela."
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title="unstuck"
        )
        return item, response, "unstuck"

    if _DELEGATE_PATTERNS.search(raw_text):
        response = await _handle_delegate(raw_text, db)
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80]
        )
        return item, response, "command"

    if _DROP_PATTERNS.search(raw_text):
        response = await _handle_drop(raw_text, db)
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title=raw_text[:80]
        )
        return item, response, "command"

    # --- F15: comando "contexto [projeto]" ---
    ctx_match = _CONTEXT_QUERY.match(raw_stripped)
    if ctx_match and db is not None:
        project_name = ctx_match.group(1).strip()
        response = await _handle_context_query(project_name, db)
        item = InboundItem(
            item_type="idea", origin=origin, raw_text=raw_text, extracted_title=f"contexto:{project_name}"
        )
        return item, response, "command"

    # Classificação normal
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


async def _route(item: InboundItem, classification: str, db: AsyncSession | None) -> str:
    if classification == "new_task":
        if db is not None:
            task = await task_manager.create(item, db)
            boss_msg = ""
            if task.is_boss_fight:
                boss_msg = f"\n⚔️ Boss fight detectado! XP triplo se vencer. Quer enfrentar hoje?"
            return f"Anotado: *{task.title}*. Prioridade: {item.priority_hint or 'normal'}.{boss_msg}"
        return f"Anotado: *{item.extracted_title}*."

    elif classification == "update":
        if db is not None:
            task, xp_msg = await task_manager.mark_done(item.extracted_title, db)
            if task:
                # F15: capturar detalhe técnico como nota na tarefa
                if _TECHNICAL_DETAIL.search(item.raw_text):
                    await _append_task_note(task, item.raw_text, db)
                return f"Show! ✅ *{task.title}* concluída.\n{xp_msg}"
        return f"Show! Tarefa '{item.extracted_title}' marcada como concluída."

    elif classification == "question":
        context = await _build_context(db)
        return await brain.answer_question(item.raw_text, context, db=db)

    elif classification == "command":
        context = await _build_context(db)
        return await brain.execute_command(item.raw_text, context, db=db)

    else:
        # F15: capturar contexto técnico em chat também
        if db is not None and _TECHNICAL_DETAIL.search(item.raw_text):
            await _capture_context_note(item.raw_text, db)
        return await brain.casual_response(item.raw_text, db=db)


async def _build_context(db: AsyncSession | None) -> str:
    if db is None:
        return "(sem contexto disponível)"
    tasks = await task_manager.get_pending(db)
    if not tasks:
        return "Nenhuma tarefa pendente."
    lines = [f"- {t.title} (prioridade {t.priority or '-'}, prazo {t.deadline or 'sem prazo'})" for t in tasks[:10]]
    return "Tarefas pendentes:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Protocolo de destravamento (ADHD)
# ---------------------------------------------------------------------------

async def _handle_unstuck_flow(raw_text: str, db: AsyncSession) -> str:
    """Gerencia o estado do protocolo de destravamento (4 passos)."""
    step = int(await task_manager.get_setting("unstuck_step", "1", db=db) or "1")

    if step == 1:
        await task_manager.set_setting("unstuck_task", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "2", db)
        return "Qual o menor pedaço que dá pra fazer em 5 minutos?"

    elif step == 2:
        await task_manager.set_setting("unstuck_micro", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "3", db)
        return "Faz só isso agora. Me avisa quando terminar. 🎯"

    elif step == 3:
        await task_manager.set_setting("unstuck_step", "4", db)
        return "Show! ✅ Quer fazer mais 5 minutos ou parar aqui?"

    else:
        # Step 4 — encerrar protocolo
        await task_manager.set_setting("unstuck_mode", "false", db)
        await task_manager.set_setting("unstuck_step", "1", db)
        if any(w in raw_text.lower() for w in ("mais", "continuar", "seguir", "sim")):
            return "Bora! Qual o próximo micro-passo?"
        return "Ótimo trabalho. Quando quiser continuar, é só falar."


# ---------------------------------------------------------------------------
# Modo crise
# ---------------------------------------------------------------------------

async def _handle_crisis_message(raw_text: str, db: AsyncSession) -> str:
    """Resposta em modo crise: suave, sem cobrança, sem mencionar backlog."""
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


# ---------------------------------------------------------------------------
# Prestige
# ---------------------------------------------------------------------------

async def _handle_prestige_accept(db: AsyncSession) -> str:
    """Reseta XP de todos os atributos principais e incrementa prestige."""
    from sqlalchemy import select
    from app.models import PlayerStat

    _PRESTIGE_ATTRIBUTES = ["craft", "strategy", "life", "willpower", "knowledge"]
    result = await db.execute(
        select(PlayerStat).where(PlayerStat.attribute.in_(_PRESTIGE_ATTRIBUTES))
    )
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


# ---------------------------------------------------------------------------
# Delegate e Drop
# ---------------------------------------------------------------------------

async def _handle_delegate(raw_text: str, db: AsyncSession | None) -> str:
    """Delega a tarefa mais recente ou extrai do contexto."""
    if db is None:
        return "Anotado! Pra quem vai delegar? (não tenho acesso ao banco agora)"
    tasks = await task_manager.get_pending(db)
    if not tasks:
        return "Sem tarefas pendentes pra delegar."
    task_to_delegate = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_delegate = t
            break
    await task_manager.delegate_task(task_to_delegate.title, "a definir", db)
    return f"*{task_to_delegate.title}* marcada como delegada. Pra quem vai? (manda o nome pra eu anotar)"


async def _handle_dump(raw_stripped: str, origin: str, db: AsyncSession | None) -> str:
    """Registra dump sem chamar Claude API. Retorna ao foco atual."""
    dump_text = _DUMP_PREFIX.sub("", raw_stripped).strip()
    if not dump_text:
        return "Dump vazio — manda o que quer registrar depois de 'dump:'"

    # Criar task com status='dump', category='backlog' sem chamar API
    if db is not None:
        from app.models import Task
        task = Task(
            title=dump_text[:500],
            origin=origin,
            status="dump",
            category="backlog",
        )
        db.add(task)
        await db.commit()

    # Buscar foco atual para redirecionar
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
            pending = await task_manager.get_pending(db)
            if pending:
                foco = pending[0].title

    return (
        f"Registrado. Isso não vai se perder.\n"
        f"Volta pro *{foco}*."
    )


async def _handle_ritual_choice(choice: str, db: AsyncSession) -> str:
    """Pedro respondeu 1/2/3 no ritual de início — salva vitória do dia."""
    from sqlalchemy import select as _select
    from app.models import DailyPlan, Task as _Task

    await task_manager.set_setting("awaiting_ritual_response", "false", db)

    # Pegar o daily_plan de hoje para saber as tarefas planejadas
    today = date.today()
    result = await db.execute(
        _select(DailyPlan).where(DailyPlan.plan_date == today)
    )
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
    """Concede Rest XP se última mensagem outbound sugeria pausa e Pedro aceitou."""
    from sqlalchemy import select as _select
    from app.models import Message as _Message

    _PAUSE_KEYWORDS = ("pausa", "descanso", "coffee break", "descanse", "respira", "break")
    last_out = await db.execute(
        _select(_Message)
        .where(_Message.direction == "outbound")
        .order_by(_Message.created_at.desc())
        .limit(1)
    )
    last_outbound = last_out.scalar_one_or_none()
    if not last_outbound:
        return
    if not any(kw in last_outbound.content.lower() for kw in _PAUSE_KEYWORDS):
        return

    stat = await task_manager.grant_xp("recovery", 10, db)
    await task_manager.set_setting("rest_xp_granted_today", "true", db)
    logger.info("Rest XP granted mid-conversation: +10 recovery (nível {})", stat.level)


async def _handle_day_off_accept(db: AsyncSession) -> str:
    """Pedro aceitou dia de respiro — setar day_off_tomorrow e day_off_bonus."""
    await task_manager.set_setting("day_off_tomorrow", "true", db)
    await task_manager.set_setting("day_off_offered", "false", db)
    return (
        "Combinado! 🌿 Amanhã é dia de respiro.\n"
        "Sem plano, sem cobranças. E na volta: 1.5x XP em tudo. 💪"
    )


async def _handle_schedule_intent(raw_text: str, db: AsyncSession) -> str:
    """
    F7 — Pedro quer reservar um bloco na agenda.
    Busca próximo slot livre hoje, sugere horário e pede confirmação.
    Salva proposta em pending_gcal_event para confirmar depois.
    """
    import json
    from datetime import datetime, timedelta, timezone
    from app.services import gcal_client

    # Tentar extrair duração do texto (ex: "1h", "2 horas", "30min")
    duration_min = 60  # default
    dur_match = re.search(r"(\d+)\s*(h(?:ora)?s?|min(?:utos?)?)", raw_text, re.IGNORECASE)
    if dur_match:
        val = int(dur_match.group(1))
        unit = dur_match.group(2).lower()
        duration_min = val * 60 if unit.startswith("h") else val

    # Extrair nome da tarefa (tudo antes dos padrões de agenda)
    task_name_match = re.sub(
        r"(?i)(reservar|agendar|bloquear na agenda|colocar na agenda|bloco pra|bloco para)\s*",
        "", raw_text
    ).strip()
    task_name = task_name_match[:100] if task_name_match else "Foco"

    # Buscar slots livres hoje
    events = await gcal_client.get_today_events()
    now = datetime.now(timezone.utc)
    # Encontrar próximo slot de work hours (09-18 BRT = 12-21 UTC)
    start_candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_work = now.replace(hour=21, minute=0, second=0, microsecond=0)  # 18h BRT

    occupied = []
    for e in events:
        try:
            from dateutil.parser import parse as _parse
            es = _parse(e["start"]).astimezone(timezone.utc)
            ee = _parse(e["end"]).astimezone(timezone.utc)
            occupied.append((es, ee))
        except Exception:
            pass

    # Buscar primeiro slot livre com duração suficiente
    slot_start = start_candidate
    slot_found = False
    for _ in range(8):  # checar até 8 horas à frente
        slot_end = slot_start + timedelta(minutes=duration_min)
        if slot_end > end_work:
            break
        conflict = any(
            not (slot_end <= os or slot_start >= oe)
            for os, oe in occupied
        )
        if not conflict:
            slot_found = True
            break
        slot_start += timedelta(hours=1)

    if not slot_found:
        return "Não achei slot livre hoje. Quer agendar pra amanhã de manhã?"

    # Formatar em BRT (UTC-3)
    slot_brt_start = slot_start - timedelta(hours=3)
    slot_brt_end = (slot_start + timedelta(minutes=duration_min)) - timedelta(hours=3)
    time_str = f"{slot_brt_start.strftime('%H:%M')} → {slot_brt_end.strftime('%H:%M')}"

    # Salvar proposta pendente
    proposal = {
        "title": task_name,
        "start": slot_start.isoformat(),
        "end": (slot_start + timedelta(minutes=duration_min)).isoformat(),
    }
    await task_manager.set_setting("pending_gcal_event", json.dumps(proposal), db)

    return (
        f"Sugiro *{time_str}* pra '{task_name}' ({duration_min}min).\n"
        f"Crio na agenda? Responde *sim* pra confirmar."
    )


async def _handle_gcal_confirm(db: AsyncSession) -> str:
    """F7 — Pedro confirmou criação do evento no GCal."""
    import json
    from datetime import datetime, timezone
    from app.services import gcal_client

    raw = await task_manager.get_setting("pending_gcal_event", db=db)
    if not raw:
        return "Não encontrei nenhum evento pendente de confirmação."

    try:
        proposal = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "Erro ao recuperar proposta de evento."

    await task_manager.set_setting("pending_gcal_event", "", db)

    start_dt = datetime.fromisoformat(proposal["start"])
    end_dt = datetime.fromisoformat(proposal["end"])
    result = await gcal_client.create_event(proposal["title"], start_dt, end_dt)

    if result.get("status") == "ok":
        return f"✅ Evento *{proposal['title']}* criado na agenda!"
    else:
        return f"Não consegui criar o evento: {result.get('error', 'erro desconhecido')}"


async def _append_task_note(task, raw_text: str, db: AsyncSession) -> None:
    """F15 — Appends technical detail to task.notes."""
    snippet = raw_text[:300].strip()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    addition = f"[{now_str}] {snippet}"
    task.notes = f"{task.notes}\n{addition}" if task.notes else addition
    await db.commit()
    logger.debug("Context note appended to task {}", task.id)


async def _capture_context_note(raw_text: str, db: AsyncSession) -> None:
    """F15 — Salva detalhe técnico de chat como nota na tarefa mais recente do projeto mencionado."""
    from sqlalchemy import select as _select
    from app.models import Task as _Task

    tasks = await task_manager.get_pending(db)
    if not tasks:
        return

    # Tentar associar a uma tarefa pelo título mencionado no texto
    raw_lower = raw_text.lower()
    matched_task = None
    for t in tasks[:10]:
        title_words = [w for w in (t.title or "").lower().split() if len(w) > 3]
        if title_words and any(w in raw_lower for w in title_words):
            matched_task = t
            break

    if not matched_task:
        return  # não conseguiu associar a projeto específico

    await _append_task_note(matched_task, raw_text, db)


async def _handle_context_query(project_name: str, db: AsyncSession) -> str:
    """F15 — Busca tasks + notes relacionadas ao projeto e gera resumo via Haiku."""
    from sqlalchemy import select as _select
    from app.models import Task as _Task, Memory as _Memory

    proj_lower = project_name.lower()

    # Tarefas com título relacionado
    tasks_result = await db.execute(
        _select(_Task)
        .where(_Task.title.ilike(f"%{project_name}%"))
        .order_by(_Task.created_at.desc())
        .limit(10)
    )
    tasks = tasks_result.scalars().all()

    # Memórias que mencionam o projeto
    mem_result = await db.execute(
        _select(_Memory)
        .where(_Memory.content.ilike(f"%{project_name}%"))
        .where(_Memory.superseded == False)  # noqa: E712
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
            # Extract relevant sentences
            for sentence in m.content.split(". "):
                if proj_lower in sentence.lower():
                    lines.append(f"  - {sentence.strip()[:150]}")
                    break

    return "\n".join(lines)


async def _handle_drop(raw_text: str, db: AsyncSession | None) -> str:
    """Dropa tarefa identificada no texto."""
    if db is None:
        return "Qual tarefa quer remover?"
    tasks = await task_manager.get_pending(db)
    if not tasks:
        return "Sem tarefas pendentes."
    task_to_drop = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_drop = t
            break
    await task_manager.drop_task(task_to_drop.title, db)
    return f"*{task_to_drop.title}* removida da lista. Sem penalidade. ✂️"
