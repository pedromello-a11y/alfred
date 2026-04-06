"""Padrões de error handling para o Alfred."""
import logging
from typing import TypeVar, Callable, Optional

logger = logging.getLogger("alfred")
T = TypeVar("T")


async def safe_async_call(fn, *args, fallback=None, context="", reraise=False, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except Exception:
        logger.exception("Erro em %s", context or fn.__name__)
        if reraise:
            raise
        return fallback


def safe_sync_call(fn, *args, fallback=None, context="", reraise=False, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("Erro em %s", context or fn.__name__)
        if reraise:
            raise
        return fallback
