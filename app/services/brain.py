"""Brain do Alfred — orquestrador de IA.
Delega chamadas de API para llm_client e construção de contexto para prompt_builder.
Este arquivo é fino: só orquestra.
"""
from __future__ import annotations

import json
import re

from loguru import logger

from app.config import settings
from app.services.llm_client import call_llm, call_llm_quick, log_llm_usage
from app.services.prompt_builder import (
    ALFRED_SYSTEM_PROMPT,
    CLASSIFIER_SYSTEM_PROMPT,
    build_conversation_context,
    _get_recent_chat_history,
)

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
# Core: _call — usa llm_client internamente
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

    messages: list[dict] = []
    if include_history and db is not None:
        try:
            history = await _get_recent_chat_history(db, limit=10, max_hours=6)
            messages.extend(history)
        except Exception as exc:
            logger.warning("Failed to load message history: {}", exc)

    messages.append({"role": "user", "content": prompt})

    system = ALFRED_SYSTEM_PROMPT
    if db is not None:
        try:
            from app.services.prompt_builder import _build_session_packet
            session_ctx = await _build_session_packet(db)
            system = system + "\n\n" + session_ctx
        except Exception as exc:
            logger.warning("Failed to build session packet: {}", exc)

    try:
        result = await call_llm(
            system_prompt=system,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        await log_llm_usage(db, result, context=call_type)
        return result["content"]
    except Exception:
        logger.exception("LLM call failed: model=%s type=%s", model, call_type)
        return _API_ERROR_RESPONSE


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

async def classify(text: str, db=None) -> dict:
    """Classifica mensagem. Tenta regex primeiro; se falhar, usa LLM."""
    regex_result = try_regex_classify(text)
    if regex_result is not None:
        return {
            "classification": regex_result,
            "extracted_title": text[:80],
            "extracted_deadline": None,
            "priority_hint": None,
        }

    raw = await _call(
        CLASSIFIER_SYSTEM_PROMPT.format(text=text),
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
        from app.services.prompt_builder import build_smart_context
        smart_ctx = await build_smart_context(question, db)
        prompt = f"Contexto:\n{smart_ctx}\n\nPergunta: {question}"
    else:
        prompt = f"Contexto:\n{context}\n\nPergunta: {question}"
    return await _call(prompt, max_tokens=300, temperature=0.3, call_type="question", db=db)


async def casual_response(message: str, db=None) -> str:
    """Resposta casual e breve. Haiku, max 150 tokens."""
    return await _call(message, max_tokens=150, temperature=0.7, call_type="casual", db=db)


async def execute_command(command: str, context: str, db=None) -> str:
    """Interpreta e confirma um comando de alteração."""
    prompt = f"Contexto:\n{context}\n\nComando: {command}"
    return await _call(prompt, max_tokens=200, temperature=0.1, call_type="command", db=db)


async def generate_briefing(context: str, db=None) -> str:
    """Gera briefing diário. Sem histórico — é geração estruturada."""
    return await _call(context, max_tokens=500, temperature=0.3, call_type="briefing", db=db, include_history=False)


async def generate_closing(context: str, db=None) -> str:
    """Gera fechamento diário. Sem histórico — é geração estruturada."""
    return await _call(context, max_tokens=400, temperature=0.3, call_type="closing", db=db, include_history=False)


async def consolidate_memory(period_type: str, raw_data: str, db=None) -> str:
    """Consolida memória diária/semanal/mensal. Sonnet, max 600 tokens."""
    prompt = f"Período: {period_type}\n\nDados:\n{raw_data}"
    return await _call(
        prompt,
        model=settings.model_smart,
        max_tokens=600,
        temperature=0.3,
        call_type=f"consolidate_{period_type}",
        db=db,
        include_history=False,
    )
