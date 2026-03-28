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
# handle — classifica via brain, normaliza para InboundItem, roteia
# Retorna (item, response_text, classification)
# ---------------------------------------------------------------------------

async def handle(
    raw_text: str, origin: str = "whatsapp", db: AsyncSession | None = None
) -> tuple["InboundItem", str, str]:
    """Classifica via Claude Haiku, roteia e retorna (item, response, classification)."""

    # Verificar se está em modo unstuck
    if db is not None:
        unstuck = await task_manager.get_setting("unstuck_mode", db=db)
        if unstuck == "true":
            response = await _handle_unstuck_flow(raw_text, db)
            item = InboundItem(
                item_type="idea",
                origin=origin,
                raw_text=raw_text,
                extracted_title=raw_text[:80],
            )
            return item, response, "unstuck"

    # Detectar padrões ADHD antes de classificar
    if _UNSTUCK_PATTERNS.search(raw_text):
        if db is not None:
            await task_manager.set_setting("unstuck_mode", "true", db)
            await task_manager.set_setting("unstuck_step", "1", db)
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
                return f"Show! ✅ *{task.title}* concluída.\n{xp_msg}"
        return f"Show! Tarefa '{item.extracted_title}' marcada como concluída."

    elif classification == "question":
        context = await _build_context(db)
        return await brain.answer_question(item.raw_text, context, db=db)

    elif classification == "command":
        context = await _build_context(db)
        return await brain.execute_command(item.raw_text, context, db=db)

    else:
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
        # Pedro respondeu com o nome da tarefa
        await task_manager.set_setting("unstuck_task", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "2", db)
        return "Qual o menor pedaço que dá pra fazer em 5 minutos?"

    elif step == 2:
        # Pedro descreveu o micro-passo
        await task_manager.set_setting("unstuck_micro", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "3", db)
        return "Faz só isso agora. Me avisa quando terminar. 🎯"

    elif step == 3:
        # Pedro avisou que terminou
        await task_manager.set_setting("unstuck_step", "4", db)
        return "Show! ✅ Quer fazer mais 5 minutos ou parar aqui?"

    else:
        # Step 4 — encerrar protocolo
        await task_manager.set_setting("unstuck_mode", "false", db)
        await task_manager.set_setting("unstuck_step", "1", db)
        if any(w in raw_text.lower() for w in ("mais", "continuar", "seguir", "sim")):
            return "Bora! Qual o próximo micro-passo?"
        return "Ótimo trabalho. Quando quiser continuar, é só falar."


async def _handle_delegate(raw_text: str, db: AsyncSession | None) -> str:
    """Delega a tarefa mais recente ou extrai do contexto."""
    if db is None:
        return "Anotado! Pra quem vai delegar? (não tenho acesso ao banco agora)"
    tasks = await task_manager.get_pending(db)
    if not tasks:
        return "Sem tarefas pendentes pra delegar."
    # Tenta encontrar tarefa mencionada no texto; senão, usa a #1
    task_to_delegate = tasks[0]
    for t in tasks:
        if t.title.lower() in raw_text.lower():
            task_to_delegate = t
            break
    await task_manager.delegate_task(task_to_delegate.title, "a definir", db)
    return f"*{task_to_delegate.title}* marcada como delegada. Pra quem vai? (manda o nome pra eu anotar)"


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
