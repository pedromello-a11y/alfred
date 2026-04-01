import json
import re
from datetime import datetime, timezone, timedelta

import anthropic
from loguru import logger
from sqlalchemy import select

from app.config import settings

_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

# ---------------------------------------------------------------------------
# Cost estimates (USD per 1M tokens, approximate)
# ---------------------------------------------------------------------------
_COST_PER_1M = {
    settings.model_fast:  {"input": 0.80,  "output": 4.00},   # Haiku 4.5
    settings.model_smart: {"input": 3.00,  "output": 15.00},  # Sonnet 4.6
}

SYSTEM_PROMPT = """\
Você é Alfred, assistente pessoal do Pedro. Você é direto, proativo e prático.

Contexto sobre o Pedro:
- Trabalha como videomaker/content producer na Hotmart, squad Operação Marcom
- Tem ADHD: precisa de micro-passos, clareza, cobrança ativa
- Projetos ativos no Jira: Galaxy, Spark, Hotmart, FIRE 26, entre outros
- Tende a procrastinar e se dispersar com muitas demandas

Seu papel:
- Organizar, priorizar e cobrar execução
- Ser breve nas mensagens (WhatsApp, não email)
- Quando Pedro trava, quebrar a tarefa em partes menores
- Quando Pedro está disperso, trazer de volta pra prioridade principal
- Reconhecer entregas e oferecer pausas
- Nunca ser passivo — sempre sugerir o próximo passo

Estilo de comunicação:
- Mensagens curtas (máx 3-4 linhas por bloco)
- Sem emoji excessivo (máx 1-2 por mensagem)
- Tom de parceiro de trabalho, não robô
- Usar formatação WhatsApp: *negrito*, _itálico_
- Nunca usar markdown (###, ```, etc)

Regras de formato:
- Use formato CABE/RISCO/PRIORIDADE/AÇÃO apenas para planejamento, conflito de prioridade ou análise de viabilidade.
- Para tudo mais, responda curto e direto.
- Quando Pedro escrever "dump:", registre, tranquilize e devolva ao foco. Nunca puxe o assunto de volta.
- Após Pedro concluir tarefa: reconheça brevemente, sugira pausa se houver tempo, não emende na próxima imediatamente.
- Quando o dia mudar (nova demanda, mudança de prioridade): recalibre silenciosamente, sem punir ou criticar a mudança.
- Pedro pode adicionar tarefas pessoais no ritual da manhã (corrida, cinema, casa). Inclua no plano sem dar mesmo peso que trabalho.
- Capture detalhes operacionais que Pedro reportar (erros, nomes, decisões) como notas da tarefa.

Regras:
- Nunca inventar tarefas que Pedro não mencionou
- Nunca inventar deadlines
- Se não sabe, perguntar
- Se Pedro sumiu por >24h, mandar check-in gentil\
"""

CLASSIFY_PROMPT = """\
Classifique esta mensagem em uma categoria. Responda APENAS com JSON, sem explicações.

Categorias:
- new_task: usuário quer registrar algo pra fazer
- update: usuário informa que completou/avançou algo
- question: usuário pergunta sobre suas tarefas/agenda
- command: usuário quer alterar algo existente (reagendar, cancelar, priorizar)
- chat: conversa geral

Mensagem: "{text}"

Responda: {{"classification": "...", "extracted_title": "...", "extracted_deadline": "...", "priority_hint": "..."}}\
"""

_API_ERROR_RESPONSE = "Estou com dificuldades técnicas, tenta de novo em 5min."

# ---------------------------------------------------------------------------
# Regex pre-classifier — economiza ~30-40% das chamadas API
# ---------------------------------------------------------------------------
_REGEX_RULES = [
    (r"(?i)(terminei|fiz|conclu[íi]|feito|pronto|acabei)", "update"),
    (r"(?i)(preciso|lembrar de|adicionar|criar tarefa|anotar|fazer)", "new_task"),
    (r"(?i)(o que tenho|pr[óo]xima tarefa|agenda|tarefas de hoje|o que fazer)", "question"),
    (r"(?i)(reagendar|cancelar|priorizar|remover|adiar)", "command"),
]


def try_regex_classify(text: str) -> str | None:
    for pattern, classification in _REGEX_RULES:
        if re.search(pattern, text):
            logger.debug("Regex classified as '{}': {}", classification, text[:60])
            return classification
    return None


# ---------------------------------------------------------------------------
# C1 — Contexto conversacional: camadas 1 e 2
# ---------------------------------------------------------------------------

async def get_recent_messages(db, limit: int = 10, max_hours: int = 6) -> list[dict]:
    """
    Camada 1: últimas `limit` mensagens OU últimas `max_hours` horas.
    Retorna lista no formato multi-turn da API Anthropic.
    """
    from app.models import Message
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    result = await db.execute(
        select(Message)
        .where(Message.created_at >= cutoff)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )
    msgs = list(reversed(result.scalars().all()))

    history = []
    for m in msgs:
        role = "user" if m.direction == "inbound" else "assistant"
        # Truncar conteúdo longo para economizar tokens
        content = m.content[:500] if m.content else ""
        history.append({"role": role, "content": content})

    # Garantir que a lista começa com "user" (requisito da API Anthropic)
    # e que não há duas mensagens consecutivas do mesmo role
    cleaned: list[dict] = []
    for msg in history:
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            # Juntar conteúdo ao invés de criar mensagem duplicada de role
            cleaned[-1]["content"] += "\n" + msg["content"]
        else:
            cleaned.append(dict(msg))

    # API Anthropic exige que primeiro seja "user"
    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)

    return cleaned


async def build_session_packet(db) -> str:
    """
    Camada 2: ~200 tokens de estado atual.
    Inclui vitória do dia, streak, modo, top 3 pendentes e fatos estáveis.
    """
    from app.models import Settings, Task, Streak

    # Vitória do dia
    victory_id = await _get_setting_value(db, "daily_victory_task_id")
    victory_title = None
    if victory_id:
        t_result = await db.execute(select(Task).where(Task.id == victory_id))
        victory_task = t_result.scalar_one_or_none()
        if victory_task:
            victory_title = victory_task.title

    # Streak
    streak_result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak_row = streak_result.scalar_one_or_none()
    streak = streak_row.streak_count if streak_row else 0

    # Modo (crise/normal)
    crisis = await _get_setting_value(db, "crisis_mode") or "false"

    # Top 3 pendentes (excluir done/cancelled/delegated/dropped/archived)
    pending_result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
        .limit(3)
    )
    top3 = pending_result.scalars().all()
    top3_str = "; ".join(t.title for t in top3) if top3 else "nenhuma"

    # Fatos estáveis (settings com prefixo "fact:")
    facts_result = await db.execute(
        select(Settings).where(Settings.key.like("fact:%"))
    )
    facts = facts_result.scalars().all()
    facts_str = "; ".join(f"{f.key[5:]}: {f.value}" for f in facts) if facts else "nenhum"

    # Day summary (mini-consolidação, se existir)
    day_summary = await _get_setting_value(db, "day_summary_latest") or ""
    summary_line = f"\n- Resumo do dia: {day_summary[:200]}" if day_summary else ""

    return (
        f"Estado atual:\n"
        f"- Vitória do dia: {victory_title or 'não definida'}\n"
        f"- Streak: {streak} dias\n"
        f"- Modo: {'crise' if crisis == 'true' else 'normal'}\n"
        f"- Top 3 pendentes: {top3_str}\n"
        f"- Fatos: {facts_str}"
        f"{summary_line}"
    )


async def _get_setting_value(db, key: str) -> str | None:
    """Helper interno — busca valor de Settings sem importar task_manager."""
    from app.models import Settings
    result = await db.execute(select(Settings).where(Settings.key == key))
    s = result.scalar_one_or_none()
    return s.value if s else None


# ---------------------------------------------------------------------------
# C6 — Smart context builder
# ---------------------------------------------------------------------------

_KNOWN_PROJECTS = ["galaxy", "spark", "fire", "hotmart", "cast", "marcom"]

_ACTIVE_STATUSES = ("pending", "in_progress")  # nunca incluir done/cancelled/delegated/dropped/archived


async def build_smart_context(question: str, db) -> str:
    """
    Filtra tarefas por projeto e período baseado na pergunta.
    Nunca inclui status done/cancelled/delegated/dropped/archived.
    """
    from app.models import Task
    lower = question.lower()

    # Detectar projeto mencionado
    project_filter = next((p for p in _KNOWN_PROJECTS if p in lower), None)

    # Detectar período
    if any(w in lower for w in ("hoje", "agora", "dia", "today")):
        from datetime import date
        today_start = datetime.combine(date.today(), datetime.min.time())
        query = (
            select(Task)
            .where(Task.status.in_(_ACTIVE_STATUSES))
            .where(Task.created_at >= today_start)
            .order_by(Task.priority.nulls_last())
            .limit(10)
        )
    elif any(w in lower for w in ("semana", "próximos", "proximos", "week")):
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        query = (
            select(Task)
            .where(Task.status.in_(_ACTIVE_STATUSES))
            .where(Task.created_at >= cutoff)
            .order_by(Task.priority.nulls_last())
            .limit(15)
        )
    else:
        query = (
            select(Task)
            .where(Task.status.in_(_ACTIVE_STATUSES))
            .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
            .limit(10)
        )

    # Aplicar filtro de projeto (busca no título)
    if project_filter:
        query = query.where(Task.title.ilike(f"%{project_filter}%"))

    result = await db.execute(query)
    tasks = result.scalars().all()

    if not tasks:
        return "Nenhuma tarefa ativa encontrada."

    lines = []
    for t in tasks:
        prazo = t.deadline.strftime("%d/%m") if t.deadline else "sem prazo"
        boss = " ⚔️" if t.is_boss_fight else ""
        lines.append(f"- {t.title} (prioridade {t.priority or '-'}, prazo {prazo}){boss}")

    return "Tarefas ativas:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Core LLM caller — agora com histórico multi-turn
# ---------------------------------------------------------------------------

async def _call(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.3,
    call_type: str = "general",
    db=None,
    include_history: bool = True,
) -> str:
    model = model or settings.model_fast

    # Montar lista de mensagens multi-turn
    messages: list[dict] = []
    if include_history and db is not None:
        try:
            history = await get_recent_messages(db, limit=10, max_hours=6)
            messages.extend(history)
        except Exception as exc:
            logger.warning("Failed to load message history: {}", exc)

    messages.append({"role": "user", "content": prompt})

    # System prompt + session packet (camada 2)
    system = SYSTEM_PROMPT
    if db is not None:
        try:
            session_ctx = await build_session_packet(db)
            system = system + "\n\n" + session_ctx
        except Exception as exc:
            logger.warning("Failed to build session packet: {}", exc)

    # Contexto para auditoria (primeiros 1000 chars do prompt + histórico resumido)
    context_summary = f"[{call_type}] prompt={prompt[:300]}"
    if len(messages) > 1:
        context_summary += f" | history={len(messages)-1} msgs"

    try:
        response = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
        )
        usage = response.usage
        logger.debug(
            "LLM call model={} type={} in={} out={} history_turns={}",
            model, call_type, usage.input_tokens, usage.output_tokens, len(messages) - 1,
        )
        await _save_usage(model, usage.input_tokens, usage.output_tokens, call_type, db, context_summary)
        return response.content[0].text
    except Exception as exc:
        logger.error("LLM call failed: {}", exc)
        return _API_ERROR_RESPONSE


async def _save_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    call_type: str,
    db,
    context_sent: str | None = None,
) -> None:
    if db is None:
        return
    try:
        rates = _COST_PER_1M.get(model, {"input": 1.0, "output": 5.0})
        cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
        from app.models import ApiUsage
        record = ApiUsage(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            call_type=call_type,
            context_sent=context_sent,
        )
        db.add(record)
        await db.commit()
    except Exception as exc:
        logger.warning("Failed to save api_usage: {}", exc)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

async def classify(text: str, db=None) -> dict:
    """Classifica mensagem. Tenta regex primeiro; se falhar, usa Claude Haiku."""
    regex_result = try_regex_classify(text)
    if regex_result is not None:
        return {
            "classification": regex_result,
            "extracted_title": text[:80],
            "extracted_deadline": None,
            "priority_hint": None,
        }

    # classify não precisa de histórico conversacional — é uma classificação técnica
    raw = await _call(
        CLASSIFY_PROMPT.format(text=text),
        max_tokens=150,
        temperature=0,
        call_type="classify",
        db=db,
        include_history=False,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("classify JSON parse failed: {}", raw)
        return {
            "classification": "chat",
            "extracted_title": "",
            "extracted_deadline": None,
            "priority_hint": None,
        }


async def answer_question(question: str, context: str, db=None) -> str:
    """Responde perguntas sobre tarefas/agenda usando smart context."""
    if db is not None:
        smart_ctx = await build_smart_context(question, db)
        prompt = f"Contexto:\n{smart_ctx}\n\nPergunta: {question}"
    else:
        prompt = f"Contexto:\n{context}\n\nPergunta: {question}"
    return await _call(prompt, max_tokens=300, temperature=0.3, call_type="question", db=db)


async def casual_response(message: str, db=None) -> str:
    """Resposta casual e breve. Haiku, max 150 tokens."""
    return await _call(message, max_tokens=150, temperature=0.7, call_type="casual", db=db)


async def execute_command(command: str, context: str, db=None) -> str:
    """Interpreta e confirma um comando de alteração. Haiku, max 200 tokens."""
    prompt = f"Contexto:\n{context}\n\nComando: {command}"
    return await _call(prompt, max_tokens=200, temperature=0.1, call_type="command", db=db)


async def generate_briefing(context: str, db=None) -> str:
    """Gera briefing diário. Haiku, max 500 tokens. Sem histórico — é geração estruturada."""
    return await _call(context, max_tokens=500, temperature=0.3, call_type="briefing", db=db, include_history=False)


async def generate_closing(context: str, db=None) -> str:
    """Gera fechamento diário. Haiku, max 400 tokens. Sem histórico — é geração estruturada."""
    return await _call(context, max_tokens=400, temperature=0.3, call_type="closing", db=db, include_history=False)


async def consolidate_memory(period_type: str, raw_data: str, db=None) -> str:
    """Consolida memória diária/semanal/mensal. Sonnet 4.6, max 600 tokens."""
    prompt = f"Período: {period_type}\n\nDados:\n{raw_data}"
    return await _call(
        prompt,
        model=settings.model_smart,
        max_tokens=600,
        temperature=0.3,
        call_type=f"consolidate_{period_type}",
        db=db,
        include_history=False,  # consolidação não usa histórico conversacional
    )
