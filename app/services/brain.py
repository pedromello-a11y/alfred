import json
import re

import anthropic
from loguru import logger

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
# Regex pre-classifier (spec: melhorias.md item 2)
# Economiza ~30-40% das chamadas API para msgs curtas e diretas.
# ---------------------------------------------------------------------------
_REGEX_RULES = [
    (r"(?i)(terminei|fiz|conclu[íi]|feito|pronto|acabei)", "update"),
    (r"(?i)(preciso|lembrar de|adicionar|criar tarefa|anotar|fazer)", "new_task"),
    (r"(?i)(o que tenho|pr[óo]xima tarefa|agenda|tarefas de hoje|o que fazer)", "question"),
    (r"(?i)(reagendar|cancelar|priorizar|remover|adiar)", "command"),
]


def try_regex_classify(text: str) -> str | None:
    """Tenta classificar por regex sem chamar API. Retorna classification ou None."""
    for pattern, classification in _REGEX_RULES:
        if re.search(pattern, text):
            logger.debug("Regex classified as '{}': {}", classification, text[:60])
            return classification
    return None


# ---------------------------------------------------------------------------
# Core LLM caller
# ---------------------------------------------------------------------------

async def _call(
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.3,
    call_type: str = "general",
    db=None,
) -> str:
    model = model or settings.model_fast
    try:
        response = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = response.usage
        logger.debug(
            "LLM call model={} type={} in={} out={}",
            model, call_type, usage.input_tokens, usage.output_tokens,
        )
        await _save_usage(model, usage.input_tokens, usage.output_tokens, call_type, db)
        return response.content[0].text
    except Exception as exc:
        logger.error("LLM call failed: {}", exc)
        return _API_ERROR_RESPONSE


async def _save_usage(
    model: str, input_tokens: int, output_tokens: int, call_type: str, db
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

    raw = await _call(
        CLASSIFY_PROMPT.format(text=text),
        max_tokens=150,
        temperature=0,
        call_type="classify",
        db=db,
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
    """Responde perguntas sobre tarefas/agenda. Haiku, max 300 tokens."""
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
    """Gera briefing diário. Haiku, max 500 tokens."""
    return await _call(context, max_tokens=500, temperature=0.3, call_type="briefing", db=db)


async def generate_closing(context: str, db=None) -> str:
    """Gera fechamento diário. Haiku, max 400 tokens."""
    return await _call(context, max_tokens=400, temperature=0.3, call_type="closing", db=db)


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
    )
