"""Testa task_manager: get_active_tasks e calculate_points."""
import pytest

from app.models import Task
from app.services import task_manager


@pytest.mark.anyio
async def test_get_active_tasks_empty(db_session):
    tasks = await task_manager.get_active_tasks(db_session)
    assert list(tasks) == []


@pytest.mark.anyio
async def test_get_active_tasks_returns_pending(db_session):
    t = Task(title="Teste", status="pending")
    db_session.add(t)
    await db_session.commit()
    tasks = await task_manager.get_active_tasks(db_session)
    assert any(x.title == "Teste" for x in tasks)


def test_calculate_points_quick():
    t = Task(title="Rápida", estimated_minutes=20)
    assert task_manager.calculate_points(t) == 5


def test_calculate_points_medium():
    t = Task(title="Média", estimated_minutes=45)
    assert task_manager.calculate_points(t) == 10


def test_calculate_points_long():
    t = Task(title="Longa", estimated_minutes=90)
    assert task_manager.calculate_points(t) == 20


def test_calculate_points_very_long():
    t = Task(title="Muito longa", estimated_minutes=200)
    assert task_manager.calculate_points(t) == 35
