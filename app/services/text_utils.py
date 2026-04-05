from __future__ import annotations

from typing import Any

_MOJIBAKE_MARKERS = ("Ã", "â", "ð", "�")


def fix_likely_mojibake(value: str) -> str:
    text = (value or "").strip()
    if not text or not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return value

    repaired = value
    for _ in range(2):
        try:
            candidate = repaired.encode("latin1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if candidate == repaired:
            break
        repaired = candidate
        if not any(marker in repaired for marker in _MOJIBAKE_MARKERS):
            break
    return repaired


def sanitize_json_strings(value: Any) -> Any:
    if isinstance(value, str):
        return fix_likely_mojibake(value)
    if isinstance(value, list):
        return [sanitize_json_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_json_strings(item) for item in value)
    if isinstance(value, dict):
        return {key: sanitize_json_strings(item) for key, item in value.items()}
    return value


def split_title(value: str) -> tuple[str, str]:
    """Separa 'Projeto | Tarefa' em (projeto, tarefa). Sem pipe retorna ('', valor)."""
    text = (value or "").strip()
    if "|" in text:
        project, title = text.split("|", 1)
        return project.strip(), title.strip()
    return "", text
