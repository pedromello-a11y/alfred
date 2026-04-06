"""Dashboard tasks — CRUD de tasks."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AgendaBlock, DumpItem, Task
from app.services import task_manager
from app.services.dashboard_helpers import (
    _humanize_deadline,
    _parse_project_task,
    _prefetch_parents,
    _serialize_deadline,
    _today_brt,
)

logger = logging.getLogger("alfred")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _task_to_flat(t: Task, parent_map: dict | None = None) -> dict:
    project = parent_map[str(t.id)][0] if (parent_map and str(t.id) in parent_map) else _parse_project_task(t.title)[0]
    task_name = t.title or ""
    checklist = getattr(t, "checklist_json", None) or []
    origin_ref = getattr(t, "origin_ref", None) or ""
    jira_key = origin_ref if (getattr(t, "origin", "") == "jira") else ""
    return {
        "id": str(t.id),
        "title": t.title,
        "taskName": task_name,
        "project": project,
        "parent_id": str(t.parent_id) if t.parent_id else None,
        "task_type": getattr(t, "task_type", "task") or "task",
        "status": t.status,
        "deadline": _serialize_deadline(t.deadline),
        "deadlineHuman": _humanize_deadline(t.deadline),
        "estimated_minutes": t.estimated_minutes,
        "on_holding": bool(getattr(t, "blocked", False)),
        "holding_reason": getattr(t, "blocked_reason", None) or "",
        "holding_until": t.blocked_until.isoformat() if getattr(t, "blocked_until", None) else None,
        "jira_key": jira_key,
        "checklistDone": sum(1 for i in checklist if i.get("done")),
        "checklistTotal": len(checklist),
    }


async def _parse_task_with_ai(raw_text: str) -> dict:
    try:
        today = _today_brt()
        dias_pt = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
        today_name = dias_pt[today.weekday()]
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        prompt = (
            f'Analise esta descrição de tarefa e extraia as informações em JSON.\n\n'
            f'Texto: "{raw_text}"\n\n'
            f'Data atual: {today.isoformat()} ({today_name})\n\n'
            'Retorne APENAS um JSON válido (sem markdown) com:\n'
            '- project: nome do projeto em maiúsculas (string ou "")\n'
            '- title: nome da tarefa (string)\n'
            '- deadline: prazo ISO 8601 ou null\n'
            '- estimate: estimativa em minutos (int, padrão 120)\n'
            '- deadlineType: "hard" se é entrega/cliente/fixo, "soft" se é meta pessoal'
        )
        msg = await client.messages.create(
            model=settings.model_fast,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except anthropic.APIConnectionError:
        logger.exception("Erro de conexão com Anthropic em _parse_task_with_ai")
        return {"project": "", "title": raw_text, "deadline": None, "estimate": 120, "deadlineType": "soft"}
    except anthropic.APIError:
        logger.exception("Erro de API Anthropic em _parse_task_with_ai")
        return {"project": "", "title": raw_text, "deadline": None, "estimate": 120, "deadlineType": "soft"}
    except Exception:
        logger.exception("Erro inesperado em _parse_task_with_ai")
        return {"project": "", "title": raw_text, "deadline": None, "estimate": 120, "deadlineType": "soft"}


@router.post("/task/create-smart")
async def create_task_smart(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    raw_text = (body.get("raw_text") or "").strip()
    if not raw_text:
        return {"status": "error", "message": "raw_text required"}
    parsed = await _parse_task_with_ai(raw_text)
    return {"parsed": parsed, "needs_confirmation": True}


@router.post("/task/create-smart/confirm")
async def confirm_smart_task(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.task_service import create_task_unified

    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "title required"}

    deadline = None
    raw_deadline = body.get("deadline")
    if raw_deadline:
        if isinstance(raw_deadline, str) and "T" not in raw_deadline:
            raw_deadline = raw_deadline + "T18:00:00"
        try:
            deadline = datetime.fromisoformat(raw_deadline)
        except ValueError:
            logger.warning("Deadline inválido em confirm_smart_task: %s", raw_deadline)

    try:
        new_task = await create_task_unified(
            db,
            title=title,
            task_type=body.get("task_type") or "task",
            parent_id=body.get("parent_id"),
            deadline=deadline,
            deadline_type=body.get("deadline_type") or "soft",
            estimated_minutes=body.get("estimate") or 120,
            origin="dashboard",
            status=body.get("status") or "active",
        )
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    return {"status": "ok", "id": str(new_task.id), "title": new_task.title}


@router.post("/task/{task_id}/checklist")
async def manage_checklist(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    checklist: list = list(getattr(task, "checklist_json", None) or [])
    action = body.get("action", "")

    if action == "add":
        text = (body.get("text") or "").strip()
        if text:
            checklist.append({"text": text, "done": False})
    elif action == "toggle":
        idx = body.get("index")
        if idx is not None and 0 <= idx < len(checklist):
            checklist[idx] = {**checklist[idx], "done": not checklist[idx].get("done", False)}
    elif action == "remove":
        idx = body.get("index")
        if idx is not None and 0 <= idx < len(checklist):
            checklist.pop(idx)
    elif action == "edit":
        idx = body.get("index", 0)
        new_text = body.get("text", "")
        if 0 <= idx < len(checklist):
            checklist[idx]["text"] = new_text

    task.checklist_json = checklist
    await db.commit()
    return {"status": "ok", "checklist": checklist}


@router.post("/task/{task_id}/note")
async def add_note(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}

    from app.services.time_utils import now_brt
    now = now_brt()
    notes_list: list = list(getattr(task, "notes_json", None) or [])
    notes_list.insert(0, {"text": text, "created_at": now.strftime("%d/%m %H:%M")})
    task.notes_json = notes_list
    await db.commit()
    return {"status": "ok", "notes": notes_list}


@router.post("/task/{task_id}/complete")
async def complete_task_v3(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    from app.services.task_service import complete_task_cascade
    actual = body.get("actual_minutes")
    await complete_task_cascade(db, task, actual_minutes=int(actual) if actual else None)
    return {"status": "ok", "title": task.title}


@router.post("/pause")
async def insert_pause(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    duration = int(body.get("duration_minutes", 15))
    from app.services.time_utils import now_brt_naive
    now = now_brt_naive()
    end = now + timedelta(minutes=duration)
    pause_block = AgendaBlock(
        title=f"Pausa {duration}min",
        start_at=now,
        end_at=end,
        block_type="break",
        source="manual",
        status="planned",
    )
    db.add(pause_block)
    await db.commit()
    return {"status": "ok", "duration_minutes": duration}


@router.post("/task/{task_id}/rename")
async def rename_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        tid = UUID(task_id)
    except Exception:
        return {"status": "error", "message": "invalid id"}
    result = await db.execute(select(Task).where(Task.id == tid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}
    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "title required"}
    task.title = title
    await db.commit()
    return {"status": "ok"}


@router.post("/task/{task_id}/update")
async def update_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        tid = UUID(task_id)
    except Exception:
        return {"status": "error", "message": "invalid id"}
    result = await db.execute(select(Task).where(Task.id == tid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "not found"}

    if "title" in body:
        task.title = (body["title"] or "").strip()
    if "status" in body:
        s = body["status"]
        if s in ("pending", "in_progress"):
            s = "active"
        task.status = s
        if s in ("done", "cancelled", "dropped"):
            from sqlalchemy import delete as sa_delete
            await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
    if "deadline" in body:
        raw = body["deadline"]
        if raw and isinstance(raw, str) and "T" not in raw:
            raw = raw + "T18:00:00"
        try:
            task.deadline = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            logger.warning("Deadline inválido em update_task: %s", body.get("deadline"))
    if "deadline_type" in body:
        task.deadline_type = body["deadline_type"]
    if "estimated_minutes" in body:
        task.estimated_minutes = body["estimated_minutes"]
    if "parent_id" in body:
        pid = body["parent_id"]
        task.parent_id = UUID(pid) if pid else None
    if "task_type" in body:
        tt = body["task_type"]
        if tt not in ("project", "deliverable", "task"):
            tt = "task"
        task.task_type = tt
    if "on_holding" in body:
        task.blocked = bool(body["on_holding"])
        if not task.blocked:
            task.status = "active"
        else:
            task.status = "on_holding"
    if "holding_reason" in body:
        task.blocked_reason = body["holding_reason"]
    if "holding_until" in body:
        val = body["holding_until"]
        try:
            from datetime import date as _date
            task.blocked_until = _date.fromisoformat(val) if val else None
        except ValueError:
            logger.warning("holding_until inválido em update_task: %s", val)

    await db.commit()
    await db.refresh(task)

    if "deadline" in body and task.deadline and task.task_type == "task":
        try:
            from app.services.scheduler import rebuild_week_schedule
            dl = task.deadline.date() if hasattr(task.deadline, "date") else task.deadline
            _today = _today_brt()
            ws = dl - timedelta(days=dl.weekday())
            we = ws + timedelta(days=4)
            if ws < _today:
                ws = _today
            await rebuild_week_schedule(db, ws, we)
        except Exception:
            logger.exception("Erro ao recalcular agenda após update_task")

    return _task_to_flat(task)


@router.post("/task/{task_id}/deadline-type")
async def update_deadline_type(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.deadline_type = body.get("deadline_type", "soft")
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/deadline")
async def update_deadline(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    dl_str = body.get("deadline", "")
    if dl_str:
        try:
            task.deadline = datetime.fromisoformat(dl_str)
        except ValueError:
            logger.warning("Deadline inválido em update_deadline: %s", dl_str)
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/block")
async def block_task(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}

    task.blocked = True
    task.blocked_reason = body.get("reason", "")
    until = body.get("blocked_until", None)
    if until:
        try:
            task.blocked_until = datetime.strptime(until, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("blocked_until inválido em block_task: %s", until)
            task.blocked_until = None
    else:
        task.blocked_until = None
    task.blocked_at = datetime.now()
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/unblock")
async def unblock_task(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}

    task.blocked = False
    task.blocked_reason = None
    task.blocked_until = None
    task.blocked_at = None
    await db.commit()
    return {"ok": True}


@router.post("/task/{task_id}/estimate")
async def update_estimate(task_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"error": "not found"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.estimated_minutes = body.get("estimated_minutes", 120)
    await db.commit()
    return {"ok": True}


@router.post("/reorder")
async def reorder_items(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    ordered_ids = body.get("ordered_ids", [])
    if not ordered_ids:
        return {"status": "ok"}

    for i, item_id in enumerate(ordered_ids):
        try:
            item_uuid = UUID(item_id)
        except (ValueError, AttributeError):
            continue
        result = await db.execute(select(Task).where(Task.id == item_uuid))
        task = result.scalar_one_or_none()
        if task:
            task.times_planned = i

    await db.commit()
    return {"status": "ok", "reordered": len(ordered_ids)}


@router.post("/create-task")
async def dashboard_create_task(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.task_service import create_task_unified
    from pydantic import BaseModel as _BM

    title = (body.get("title") or "").strip()
    if not title:
        return {"status": "error", "message": "title is required"}

    deadline = None
    if body.get("date"):
        try:
            deadline = datetime.fromisoformat(body["date"])
        except ValueError:
            logger.warning("Data inválida em create-task: %s", body.get("date"))

    try:
        new_task = await create_task_unified(db, title=title, deadline=deadline, origin="dashboard")
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    return {"status": "ok", "id": str(new_task.id), "title": new_task.title}


@router.post("/task-edit")
async def dashboard_task_edit(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    task_id = body.get("task_id", "")
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}
    title = (body.get("title") or "").strip()
    project = (body.get("project") or "").strip()
    if title:
        full_title = f"{project} | {title}" if project else title
        if hasattr(task_manager, "canonicalize_task_title"):
            task.title = task_manager.canonicalize_task_title(full_title)
        else:
            task.title = full_title
    if body.get("date"):
        try:
            task.deadline = datetime.fromisoformat(body["date"])
        except ValueError:
            logger.warning("Data inválida em task-edit: %s", body.get("date"))
    if body.get("note"):
        from datetime import timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = f"[{ts}] {body['note'].strip()}"
        task.notes = f"{task.notes}\n{entry}" if task.notes else entry
    await db.commit()
    return {"status": "ok", "title": task.title}


@router.post("/action")
async def dashboard_action(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import timezone
    task_id = body.get("task_id", "")
    action = (body.get("action") or "").lower().strip()
    note = body.get("note")
    date_str = body.get("date")

    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}
    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}

    from sqlalchemy import delete as sa_delete
    if action in ("concluida", "concluída", "done"):
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc)
        if note:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            entry = f"[{ts}] {note.strip()}"
            task.notes = f"{task.notes}\n{entry}" if task.notes else entry
        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
        await db.commit()
        return {"status": "ok", "action": "done", "title": task.title}
    if action in ("excluir", "delete", "remover"):
        task.status = "cancelled"
        await db.execute(sa_delete(AgendaBlock).where(AgendaBlock.task_id == task.id))
        await db.commit()
        return {"status": "ok", "action": "cancelled", "title": task.title}
    if action in ("adiar", "postpone") and date_str:
        try:
            task.deadline = datetime.fromisoformat(date_str)
        except ValueError:
            logger.warning("Data inválida em action adiar: %s", date_str)
        await db.commit()
        return {"status": "ok", "action": "postponed", "title": task.title}
    return {"status": "error", "message": f"unknown action: {action}"}


@router.post("/input")
async def alfred_unified_input(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import ChatMessage
    text = (body.get("text") or "").strip()
    if not text:
        return {"type": "error", "message": "texto vazio"}

    try:
        hist_result = await db.execute(
            select(ChatMessage).order_by(ChatMessage.created_at.desc()).limit(10)
        )
        history = list(reversed(hist_result.scalars().all()))
    except Exception:
        logger.exception("Erro ao carregar histórico de chat em unified_input")
        history = []

    tasks_result = await db.execute(
        select(Task).where(Task.status.in_(("active", "pending", "in_progress")))
        .order_by(Task.deadline.asc().nulls_last()).limit(20)
    )
    current_tasks = tasks_result.scalars().all()
    tasks_context = "\n".join([f"- {t.title} (id:{t.id}, deadline:{t.deadline})" for t in current_tasks])

    history_text = "\n".join([
        f"{'Usuário' if m.role == 'user' else 'Alfred'}: {m.content}" for m in history
    ]) if history else ""

    today = _today_brt()
    prompt = f"""Você é o Alfred, assistente de produtividade pessoal. Classifique a intenção do usuário.

Tarefas ativas:
{tasks_context or '(nenhuma)'}

Histórico recente:
{history_text or '(nenhum)'}

Data atual: {today.isoformat()}

Entrada do usuário: "{text}"

Responda APENAS com JSON válido (sem markdown):
{{
  "intent": "create_task" | "update_task" | "complete_task" | "create_dump" | "query" | "unclear",
  "task_title": "título da tarefa se criar",
  "project": "nome do projeto se detectado",
  "deadline": "YYYY-MM-DD se mencionado ou null",
  "target_task_id": "uuid da task existente se update/complete",
  "dump_text": "texto se for dump/anotação",
  "message": "resposta para o usuário"
}}"""

    result_data: dict = {"type": "error", "message": "Erro interno"}
    intent = "unclear"

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model=settings.model_fast if hasattr(settings, 'model_fast') else "claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = msg.content[0].text.strip()
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        parsed = json.loads(response_text.strip())
        intent = parsed.get("intent", "unclear")

        if intent == "create_task":
            from app.services.task_service import create_task_unified
            title = (parsed.get("task_title") or text).strip()
            deadline = None
            if parsed.get("deadline"):
                try:
                    deadline = datetime.fromisoformat(parsed["deadline"] + "T18:00:00")
                except ValueError:
                    logger.warning("Deadline inválido do AI em unified_input: %s", parsed.get("deadline"))
            new_task = await create_task_unified(
                db, title=title, task_type="task", deadline=deadline,
                origin="alfred_input",
            )
            result_data = {"type": "task_created", "id": str(new_task.id), "title": new_task.title,
                      "deadline": _serialize_deadline(deadline),
                      "message": f"Tarefa criada: {new_task.title}"}

        elif intent == "complete_task":
            from app.services.task_service import complete_task_cascade
            task_id = parsed.get("target_task_id")
            if task_id:
                try:
                    _r = await db.execute(select(Task).where(Task.id == UUID(task_id)))
                    _t = _r.scalar_one_or_none()
                    if _t:
                        await complete_task_cascade(db, _t)
                        result_data = {"type": "task_completed", "title": _t.title,
                                  "message": f"✅ Concluída: {_t.title}"}
                    else:
                        result_data = {"type": "clarification", "message": "Tarefa não encontrada."}
                except Exception:
                    logger.exception("Erro ao completar task em unified_input")
                    result_data = {"type": "clarification", "message": "Qual tarefa você quer concluir?"}
            else:
                result_data = {"type": "clarification", "message": parsed.get("message", "Qual tarefa concluir?")}

        elif intent == "create_dump":
            dump_text = parsed.get("dump_text") or text
            new_dump = DumpItem(raw_text=dump_text, rewritten_title=dump_text[:100],
                                status="categorized", source="alfred_input", category="anotacao")
            db.add(new_dump)
            await db.commit()
            result_data = {"type": "dump_saved", "text": dump_text, "message": f"Anotado: {dump_text[:60]}"}

        elif intent == "update_task":
            task_id = parsed.get("target_task_id")
            if task_id:
                try:
                    _r = await db.execute(select(Task).where(Task.id == UUID(task_id)))
                    _t = _r.scalar_one_or_none()
                    if _t:
                        if parsed.get("deadline"):
                            try:
                                _t.deadline = datetime.fromisoformat(parsed["deadline"] + "T18:00:00")
                            except ValueError:
                                logger.warning("Deadline inválido do AI em update_task: %s", parsed.get("deadline"))
                        await db.commit()
                        result_data = {"type": "task_updated", "title": _t.title,
                                  "message": f"Atualizado: {_t.title}"}
                    else:
                        result_data = {"type": "clarification", "message": "Tarefa não encontrada."}
                except Exception:
                    logger.exception("Erro ao atualizar task em unified_input")
                    result_data = {"type": "clarification", "message": parsed.get("message", "Qual tarefa alterar?")}
            else:
                result_data = {"type": "clarification", "message": parsed.get("message", "Qual tarefa alterar?")}

        elif intent == "query":
            result_data = {"type": "query_response", "message": parsed.get("message", "Consulta processada.")}

        else:
            result_data = {"type": "clarification", "message": parsed.get("message", "Não entendi. Tente: 'criar tarefa X' ou 'anotar Y'")}

    except anthropic.APIConnectionError:
        logger.exception("Erro de conexão com Anthropic em unified_input")
        result_data = {"type": "error", "message": "Erro de conexão com AI. Tente novamente."}
    except anthropic.APIError:
        logger.exception("Erro de API Anthropic em unified_input")
        result_data = {"type": "error", "message": "Erro na API AI. Tente novamente."}
    except Exception:
        logger.exception("Erro inesperado em unified_input")
        result_data = {"type": "error", "message": "Erro interno."}

    try:
        from app.models import ChatMessage as _CM
        db.add(_CM(role="user", content=text))
        db.add(_CM(role="assistant", content=result_data.get("message", ""), intent=intent, result_data=result_data))
        await db.commit()
    except Exception:
        logger.exception("Erro ao salvar histórico de chat em unified_input")

    return result_data


@router.post("/migrate-clean-titles")
async def migrate_clean_titles(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task))
    tasks = result.scalars().all()
    cleaned = 0
    for task in tasks:
        if not task.title or "|" not in task.title:
            continue
        parts = [p.strip() for p in task.title.split("|")]
        task.title = parts[-1]
        cleaned += 1
    await db.commit()
    return {"status": "ok", "cleaned": cleaned}
