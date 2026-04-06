"""Auxiliares de blocos — funções de risco e helper de busca.

As funções de alocação/geração foram removidas.
A engine única de agenda é app/services/scheduler.py → rebuild_week_schedule().
"""
from __future__ import annotations

from datetime import date, datetime

from app.models import AgendaBlock, Task


def _parse_project_task(title: str) -> tuple[str, str]:
    if "|" in (title or ""):
        p, t = title.split("|", 1)
        return p.strip(), t.strip()
    return "", (title or "").strip()


def _task_priority_key(task: Task, today: date) -> tuple:
    """Ordena: vencidas → prazo hoje → esta semana → próxima semana → sem prazo."""
    if not task.deadline:
        return (4, 9999, task.priority or 5)
    try:
        dl = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
        delta = (dl - today).days
    except Exception:
        return (4, 9999, task.priority or 5)
    if delta < 0:
        group = 0
    elif delta == 0:
        group = 1
    elif delta <= 6:
        group = 2
    elif delta <= 13:
        group = 3
    else:
        group = 4
    return (group, delta, task.priority or 5)


def _calc_risk(
    tasks: list[Task],
    suggested: list[dict],
    today: date,
    week_end: date,
) -> dict | None:
    """Calcula risco da semana considerando bloqueadas e datas de desbloqueio."""
    needed_min = 0
    risky_tasks: list[dict] = []

    for t in tasks:
        if getattr(t, "blocked", False):
            until = getattr(t, "blocked_until", None)
            if until and until <= week_end:
                est = (getattr(t, "estimated_minutes", 120) or 120)
                needed_min += est
                risky_tasks.append({
                    "name": t.title,
                    "hours": est / 60,
                    "deadline_type": getattr(t, "deadline_type", "soft") or "soft",
                    "note": f"desbloqueada a partir de {until}",
                })
            continue

        if not t.deadline:
            continue
        try:
            dl = t.deadline.date() if hasattr(t.deadline, "date") else t.deadline
        except Exception:
            continue
        if dl <= week_end:
            est = t.estimated_minutes or 120
            needed_min += est
            risky_tasks.append({
                "name": t.title,
                "hours": est / 60,
                "deadline_type": getattr(t, "deadline_type", "soft") or "soft",
            })

    if needed_min == 0:
        return None

    available_min = sum(
        int(
            (
                datetime.strptime(b["end"], "%H:%M") - datetime.strptime(b["start"], "%H:%M")
            ).total_seconds() / 60
        )
        for b in suggested
        if b.get("type") != "quick"
    )

    needed_h = round(needed_min / 60, 1)
    available_h = round(available_min / 60, 1)
    deficit = round(max(0, needed_h - available_h), 1)

    if deficit <= 0:
        return None

    suggestion = ""
    soft_tasks = [rt for rt in risky_tasks if rt.get("deadline_type") != "hard"]
    if soft_tasks:
        easiest = min(soft_tasks, key=lambda x: x["hours"])
        name = easiest["name"]
        if "|" in name:
            _, name = name.split("|", 1)
        name = name.strip()
        suggestion = f"Considere mover '{name}' ({easiest['hours']:.0f}h) pra próxima semana."
    else:
        suggestion = f"Déficit de {deficit}h esta semana. Considere adiar tarefas ou trabalhar além do horário padrão."

    return {
        "totalHoursNeeded": needed_h,
        "totalHoursAvailable": available_h,
        "deficit": deficit,
        "suggestion": suggestion,
        "taskCount": len(risky_tasks),
    }


def find_next_block_for_task(suggested: list[dict], task_id: str) -> str:
    """Retorna o horário de início do próximo bloco sugerido para uma task."""
    for block in suggested:
        if block.get("taskId") == task_id:
            return block.get("start", "")
    return ""
