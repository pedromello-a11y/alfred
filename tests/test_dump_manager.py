"""Testa dump_manager: classificação por categoria."""
from app.services.dump_manager import classify_dump


def test_classify_filme():
    result = classify_dump("assistir o filme Duna parte 2")
    assert result.category in {"entretenimento", "filmes_series", "lazer", "filme", "filmes"}


def test_classify_compra():
    result = classify_dump("comprar notebook novo")
    assert result.category in {"compras", "pessoal", "financeiro"}


def test_classify_returns_title():
    result = classify_dump("ler o livro Sapiens")
    assert result.rewritten_title
    assert len(result.rewritten_title) > 0
