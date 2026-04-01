"""Testa inbound_handler: ok, vazio, duplicado."""
import pytest

from app.services import inbound_handler


@pytest.mark.anyio
async def test_inbound_empty_text(db_session):
    result = await inbound_handler.process_inbound("", db_session)
    assert result["status"] == "ignored"


@pytest.mark.anyio
async def test_inbound_whitespace_only(db_session):
    result = await inbound_handler.process_inbound("   ", db_session)
    assert result["status"] == "ignored"


@pytest.mark.anyio
async def test_inbound_ok(db_session):
    result = await inbound_handler.process_inbound("olá alfred", db_session, message_id="test-001")
    assert result["status"] == "ok"
    assert result["reply"] is not None


@pytest.mark.anyio
async def test_inbound_duplicate(db_session):
    await inbound_handler.process_inbound("olá alfred", db_session, message_id="dup-001")
    result = await inbound_handler.process_inbound("olá alfred", db_session, message_id="dup-001")
    assert result["status"] == "duplicate"
