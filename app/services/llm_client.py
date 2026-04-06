"""Cliente LLM — único módulo que importa anthropic."""
from __future__ import annotations

import logging
import time
from typing import Any

import anthropic

from app.config import settings

logger = logging.getLogger("alfred")

_client: anthropic.AsyncAnthropic | None = None

_COST_PER_1M = {
    settings.model_fast:  {"input": 0.80,  "output": 4.00},
    settings.model_smart: {"input": 3.00,  "output": 15.00},
}

_API_ERROR_RESPONSE = "Estou com dificuldades técnicas, tenta de novo em 5min."


def get_client() -> anthropic.AsyncAnthropic:
    """Singleton AsyncAnthropic."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def call_llm(
    system_prompt: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 300,
    temperature: float = 0.3,
) -> dict:
    """Chama a API e retorna dict com content/tokens/cost/latency."""
    model = model or settings.model_fast
    client = get_client()
    t0 = time.monotonic()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=messages,
        )
        latency = time.monotonic() - t0
        usage = response.usage
        rates = _COST_PER_1M.get(model, {"input": 1.0, "output": 5.0})
        cost = (usage.input_tokens * rates["input"] + usage.output_tokens * rates["output"]) / 1_000_000
        return {
            "content": response.content[0].text,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": cost,
            "latency_s": round(latency, 3),
            "model": model,
        }
    except anthropic.APIConnectionError as exc:
        logger.error("LLM connection error model=%s: %s", model, exc)
        raise
    except anthropic.APIError as exc:
        logger.error("LLM API error model=%s: %s", model, exc)
        raise
    except Exception:
        logger.exception("LLM unexpected error model=%s", model)
        raise


async def call_llm_quick(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 300,
    model: str | None = None,
) -> str:
    """Shortcut para chamada single-turn. Retorna string ou fallback."""
    try:
        result = await call_llm(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            model=model or settings.model_fast,
            max_tokens=max_tokens,
        )
        return result["content"]
    except Exception:
        return _API_ERROR_RESPONSE


async def log_llm_usage(db: Any, result: dict, context: str = "") -> None:
    """Persiste uso de LLM na tabela api_usage."""
    if db is None:
        return
    try:
        from app.models import ApiUsage
        record = ApiUsage(
            model=result.get("model", ""),
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
            estimated_cost_usd=result.get("cost_usd", 0.0),
            call_type=context,
            context_sent=context[:500] if context else None,
        )
        db.add(record)
        await db.commit()
    except Exception:
        logger.warning("Falha ao salvar api_usage para contexto '%s'", context)
