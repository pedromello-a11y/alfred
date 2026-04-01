"""Testa o classificador regex do brain (sem chamar Claude)."""
import pytest

from app.services.brain import try_regex_classify


def test_classify_new_task():
    result = try_regex_classify("preciso fazer o relatório até sexta")
    assert result == "new_task"


def test_classify_question():
    result = try_regex_classify("próxima tarefa")
    assert result == "question"


def test_classify_chat_fallback():
    result = try_regex_classify("saudações aleatórias xyzabc")
    assert result is None or result == "chat"
