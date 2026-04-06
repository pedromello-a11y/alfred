"""Constantes de prompts e builders de contexto."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("alfred")

_KNOWN_PROJECTS = ["galaxy", "spark", "fire", "hotmart", "cast", "marcom"]

# ── System prompts ──────────────────────────────────────────────────────────

ALFRED_SYSTEM_PROMPT = """\
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

CLASSIFIER_SYSTEM_PROMPT = """\
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


# ── Context builders ────────────────────────────────────────────────────────

async def _get_recent_chat_history(db, limit: int = 10, max_hours: int = 6) -> list[dict]:
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
        content = m.content[:500] if m.content else ""
        history.append({"role": role, "content": content})

    cleaned: list[dict] = []
    for msg in history:
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            cleaned[-1]["content"] += "\n" + msg["content"]
        else:
            cleaned.append(dict(msg))

    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)

    return cleaned


async def _build_task_summary(db) -> str:
    from app.constants import ACTIVE_STATUSES
    from app.models import Task
    result = await db.execute(
        select(Task)
        .where(Task.status.in_(ACTIVE_STATUSES))
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
        .limit(10)
    )
    tasks = result.scalars().all()
    if not tasks:
        return "Nenhuma tarefa ativa."
    lines = []
    for t in tasks:
        prazo = t.deadline.strftime("%d/%m") if t.deadline else "sem prazo"
        lines.append(f"- {t.title} (prazo {prazo})")
    return "Tarefas ativas:\n" + "\n".join(lines)


async def _get_relevant_memories(db) -> str:
    from app.models import Settings
    result = await db.execute(
        select(Settings).where(Settings.key.like("fact:%"))
    )
    facts = result.scalars().all()
    if not facts:
        return ""
    return "; ".join(f"{f.key[5:]}: {f.value}" for f in facts)


async def _build_session_packet(db) -> str:
    """~200 tokens de estado atual: vitória, streak, modo, top 3 pendentes, fatos."""
    from app.models import Settings, Task, Streak

    async def _get(key: str) -> str | None:
        result = await db.execute(select(Settings).where(Settings.key == key))
        s = result.scalar_one_or_none()
        return s.value if s else None

    victory_id = await _get("daily_victory_task_id")
    victory_title = None
    if victory_id:
        t_result = await db.execute(select(Task).where(Task.id == victory_id))
        vt = t_result.scalar_one_or_none()
        if vt:
            victory_title = vt.title

    streak_result = await db.execute(
        select(Streak).order_by(Streak.streak_date.desc()).limit(1)
    )
    streak_row = streak_result.scalar_one_or_none()
    streak = streak_row.streak_count if streak_row else 0

    crisis = await _get("crisis_mode") or "false"

    pending_result = await db.execute(
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
        .limit(3)
    )
    top3 = pending_result.scalars().all()
    top3_str = "; ".join(t.title for t in top3) if top3 else "nenhuma"

    facts_result = await db.execute(
        select(Settings).where(Settings.key.like("fact:%"))
    )
    facts = facts_result.scalars().all()
    facts_str = "; ".join(f"{f.key[5:]}: {f.value}" for f in facts) if facts else "nenhum"

    day_summary = await _get("day_summary_latest") or ""
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


async def build_smart_context(question: str, db) -> str:
    """Filtra tarefas por projeto e período baseado na pergunta."""
    from app.constants import ACTIVE_STATUSES
    from app.models import Task

    lower = question.lower()
    project_filter = next((p for p in _KNOWN_PROJECTS if p in lower), None)

    if any(w in lower for w in ("hoje", "agora", "dia", "today")):
        from datetime import date
        today_start = datetime.combine(date.today(), datetime.min.time())
        query = (
            select(Task)
            .where(Task.status.in_(ACTIVE_STATUSES))
            .where(Task.created_at >= today_start)
            .order_by(Task.priority.nulls_last())
            .limit(10)
        )
    elif any(w in lower for w in ("semana", "próximos", "proximos", "week")):
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        query = (
            select(Task)
            .where(Task.status.in_(ACTIVE_STATUSES))
            .where(Task.created_at >= cutoff)
            .order_by(Task.priority.nulls_last())
            .limit(15)
        )
    else:
        query = (
            select(Task)
            .where(Task.status.in_(ACTIVE_STATUSES))
            .order_by(Task.priority.nulls_last(), Task.deadline.nulls_last())
            .limit(10)
        )

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


async def build_conversation_context(db, user_message: str, max_history: int = 10) -> dict:
    """Monta contexto completo para uma chamada conversacional."""
    history: list[dict] = []
    task_summary = ""
    memories = ""

    try:
        history = await _get_recent_chat_history(db, limit=max_history)
    except Exception:
        logger.warning("Falha ao carregar histórico de mensagens")

    try:
        task_summary = await _build_task_summary(db)
    except Exception:
        logger.warning("Falha ao construir task summary")

    try:
        memories = await _get_relevant_memories(db)
    except Exception:
        logger.warning("Falha ao buscar memories")

    return {
        "history": history,
        "task_summary": task_summary,
        "memories": memories,
        "user_message": user_message,
    }
