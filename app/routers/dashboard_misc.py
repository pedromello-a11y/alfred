"""Dashboard misc — dumps, personal, jira write, fix/cleanup."""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import AgendaBlock, DumpItem, Task
from app.services.dashboard_helpers import (
    _humanize_deadline,
    _parse_project_task,
    _serialize_deadline,
)

logger = logging.getLogger("alfred")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _jira_configured() -> bool:
    return bool(settings.jira_base_url and settings.jira_email and settings.jira_api_token)


def _jira_auth_headers() -> dict:
    token = base64.b64encode(
        f"{settings.jira_email}:{settings.jira_api_token}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── Dump endpoints ──────────────────────────────────────────────────────────

@router.post("/dump")
async def create_quick_dump(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    category = (body.get("type") or body.get("category") or "anotacao").strip()
    new_dump = DumpItem(
        raw_text=text,
        rewritten_title=text[:100],
        status="categorized",
        source="dashboard",
        category=category,
    )
    db.add(new_dump)
    await db.commit()
    await db.refresh(new_dump)
    return {"status": "ok", "id": str(new_dump.id)}


@router.post("/dump/{dump_id}/delete")
async def delete_dump_item(dump_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        dump_uuid = UUID(dump_id)
    except ValueError:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid))
    item = result.scalar_one_or_none()
    if not item:
        return {"error": "not found"}
    await db.delete(item)
    await db.commit()
    return {"ok": True}


@router.post("/dump/{dump_id}/edit")
async def edit_dump_item(dump_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        dump_uuid = UUID(dump_id)
    except ValueError:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid))
    item = result.scalar_one_or_none()
    if not item:
        return {"error": "not found"}
    if "rewritten_title" in body:
        item.rewritten_title = body["rewritten_title"]
    if "category" in body:
        item.category = body["category"]
    if "notes" in body:
        item.notes = body["notes"]
    await db.commit()
    return {"ok": True}


@router.post("/dump/{dump_id}/convert-to-task")
async def convert_dump_to_task(dump_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.task_service import create_task_unified
    try:
        dump_uuid = UUID(dump_id)
    except ValueError:
        return {"status": "error", "message": "invalid dump_id"}

    dump_result = await db.execute(select(DumpItem).where(DumpItem.id == dump_uuid))
    dump = dump_result.scalar_one_or_none()
    if not dump:
        return {"status": "error", "message": "dump not found"}

    title = (body.get("title") or dump.rewritten_title or dump.raw_text or "").strip()
    if not title:
        return {"status": "error", "message": "title required"}

    deadline = None
    raw_dl = body.get("deadline")
    if raw_dl:
        if isinstance(raw_dl, str) and "T" not in raw_dl:
            raw_dl = raw_dl + "T18:00:00"
        try:
            deadline = datetime.fromisoformat(raw_dl)
        except ValueError:
            logger.warning("deadline inválido em convert_dump_to_task: %s", raw_dl)

    notes_initial = f"[Dump original] {dump.raw_text or ''}"
    if dump.summary:
        notes_initial += f"\n[Resumo] {dump.summary}"

    try:
        new_task = await create_task_unified(
            db,
            title=title,
            task_type=body.get("task_type") or "task",
            parent_id=body.get("parent_id"),
            deadline=deadline,
            estimated_minutes=body.get("estimate") or 120,
            origin="dump_converted",
            notes_initial=notes_initial,
        )
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    dump.status = "converted"
    await db.commit()

    return {"status": "ok", "task_id": str(new_task.id), "title": new_task.title, "dump_id": str(dump.id)}


@router.get("/dumps")
async def get_dumps_v2(db: AsyncSession = Depends(get_db)) -> list:
    result = await db.execute(
        select(DumpItem).where(DumpItem.status != "archived").order_by(DumpItem.created_at.desc())
    )
    items = result.scalars().all()
    return [
        {
            "id": str(d.id),
            "title": d.rewritten_title or (d.raw_text[:100] if d.raw_text else ""),
            "raw_text": d.raw_text,
            "category": d.category,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in items
    ]


@router.post("/dumps")
async def create_dump(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"error": "text required"}
    d = DumpItem(raw_text=text, rewritten_title=text, status="categorized", source="dashboard")
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return {"id": str(d.id), "title": d.rewritten_title}


@router.put("/dumps/{dump_id}")
async def update_dump(dump_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        did = UUID(dump_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == did))
    d = result.scalar_one_or_none()
    if not d:
        return {"error": "not found"}
    if "title" in body:
        d.rewritten_title = body["title"]
        d.raw_text = body["title"]
    if "category" in body:
        d.category = body["category"]
    await db.commit()
    return {"ok": True}


@router.delete("/dumps/{dump_id}")
async def delete_dump(dump_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    try:
        did = UUID(dump_id)
    except Exception:
        return {"error": "invalid id"}
    result = await db.execute(select(DumpItem).where(DumpItem.id == did))
    d = result.scalar_one_or_none()
    if not d:
        return {"error": "not found"}
    await db.delete(d)
    await db.commit()
    return {"ok": True}


# ── Personal (Task-based) endpoints ────────────────────────────────────────

@router.post("/personal")
async def create_personal_item(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    category = (body.get("category") or "personal").strip()
    if not category.startswith("personal"):
        category = f"personal_{category}"
    new_task = Task(title=text, origin="dashboard", status="pending", category=category)
    if hasattr(new_task, "checklist_json"):
        new_task.checklist_json = []
    if hasattr(new_task, "notes_json"):
        new_task.notes_json = []
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)
    return {"status": "ok", "id": str(new_task.id)}


@router.post("/personal/{item_id}/edit")
async def edit_personal_item(item_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    task.title = body.get("title", task.title)
    await db.commit()
    return {"ok": True}


@router.post("/personal/{item_id}/toggle")
async def toggle_personal_item(item_id: str, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    done = body.get("done", False)
    task.status = "done" if done else "pending"
    await db.commit()
    return {"ok": True}


@router.post("/personal/{item_id}/delete")
async def delete_personal_item(item_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(Task).where(Task.id == item_id))
    task = result.scalar_one_or_none()
    if not task:
        return {"error": "not found"}
    await db.delete(task)
    await db.commit()
    return {"ok": True}


@router.post("/personal/reorder")
async def reorder_personal(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    ids = body.get("ids", [])
    for i, item_id in enumerate(ids):
        try:
            item_uuid = UUID(item_id)
        except ValueError:
            continue
        result = await db.execute(select(Task).where(Task.id == item_uuid))
        task = result.scalar_one_or_none()
        if task:
            task.times_planned = i
    await db.commit()
    return {"ok": True}


# ── Jira write endpoints ────────────────────────────────────────────────────

@router.post("/jira/link")
async def jira_link_task(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}
    task_id = body.get("task_id", "")
    jira_key = body.get("jira_key", "")
    try:
        task_uuid = UUID(task_id)
    except ValueError:
        return {"status": "error", "message": "invalid task_id"}

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        return {"status": "error", "message": "task not found"}

    url = f"{settings.jira_base_url.rstrip('/')}/rest/api/2/issue/{jira_key}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_jira_auth_headers())
            resp.raise_for_status()
            issue = resp.json()
    except Exception as e:
        logger.exception("Erro ao acessar Jira em jira_link_task")
        return {"status": "error", "message": str(e)}

    fields = issue.get("fields", {})
    updated_fields = []

    task.origin_ref = jira_key
    task.origin = "jira"
    updated_fields.append("jira_key")

    if not task.deadline and fields.get("duedate"):
        try:
            task.deadline = datetime.fromisoformat(fields["duedate"])
            updated_fields.append("deadline")
        except ValueError:
            logger.warning("duedate inválido do Jira: %s", fields.get("duedate"))

    description_raw = fields.get("description") or {}
    desc_text = ""
    if isinstance(description_raw, dict):
        try:
            for block in description_raw.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        desc_text += inline.get("text", "")
        except Exception:
            logger.warning("Erro ao parsear descrição do Jira em jira_link_task")
    elif isinstance(description_raw, str):
        desc_text = description_raw

    if desc_text.strip():
        notes = list(task.notes_json or [])
        notes.append({
            "text": f"[Jira] {desc_text[:500]}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        task.notes_json = notes
        updated_fields.append("notes")

    await db.commit()
    return {"status": "ok", "updated_fields": updated_fields}


@router.post("/jira/import")
async def jira_import_issues(body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    if not _jira_configured():
        return {"status": "error", "message": "Jira não configurado"}
    keys = body.get("keys", [])
    if not keys:
        return {"status": "error", "message": "no keys provided"}

    imported_tasks = []
    for key in keys:
        url = f"{settings.jira_base_url.rstrip('/')}/rest/api/2/issue/{key}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    url,
                    headers=_jira_auth_headers(),
                    params={"fields": "summary,status,duedate,priority,description"},
                )
                resp.raise_for_status()
                issue = resp.json()
        except Exception as e:
            logger.exception("Erro ao importar issue Jira %s", key)
            imported_tasks.append({"key": key, "status": "error", "message": str(e)})
            continue

        fields = issue.get("fields", {})
        summary = fields.get("summary", key)

        deadline = None
        if fields.get("duedate"):
            try:
                deadline = datetime.fromisoformat(fields["duedate"])
            except ValueError:
                logger.warning("duedate inválido do Jira issue %s: %s", key, fields.get("duedate"))

        description_raw = fields.get("description") or {}
        desc_text = ""
        if isinstance(description_raw, dict):
            try:
                for block in description_raw.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            desc_text += inline.get("text", "")
            except Exception:
                logger.warning("Erro ao parsear descrição do Jira issue %s", key)
        elif isinstance(description_raw, str):
            desc_text = description_raw

        from app.services.task_service import create_task_unified
        notes_initial = f"[Jira] {desc_text[:500]}" if desc_text.strip() else None
        try:
            new_task = await create_task_unified(
                db,
                title=summary,
                task_type="task",
                deadline=deadline,
                estimated_minutes=120,
                origin="jira",
                origin_ref=key,
                notes_initial=notes_initial,
            )
            imported_tasks.append({
                "key": key, "status": "imported",
                "id": str(new_task.id), "title": summary,
            })
        except Exception as e:
            logger.exception("Erro ao criar task de Jira issue %s", key)
            imported_tasks.append({"key": key, "status": "error", "message": str(e)})
            continue

    imported_count = sum(1 for t in imported_tasks if t.get("status") == "imported")
    return {"imported": imported_count, "tasks": imported_tasks}


# ── Fix / cleanup endpoints ─────────────────────────────────────────────────

@router.post("/fix/cleanup")
async def fix_cleanup(db: AsyncSession = Depends(get_db)) -> dict:
    from sqlalchemy import delete as sa_delete

    valid_result = await db.execute(
        select(Task.id).where(Task.status.notin_(["done", "cancelled", "dropped"]))
    )
    valid_ids = {row[0] for row in valid_result.all()}

    orphan_result = await db.execute(
        select(AgendaBlock).where(AgendaBlock.task_id.isnot(None))
    )
    deleted_blocks = 0
    for block in orphan_result.scalars().all():
        if block.task_id not in valid_ids:
            await db.delete(block)
            deleted_blocks += 1

    all_tasks_result = await db.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled", "dropped"]))
        .order_by(Task.created_at.asc())
    )
    seen_titles: dict = {}
    deleted_dupes = 0
    for task in all_tasks_result.scalars().all():
        key = (task.title or "").strip().lower()
        if key in seen_titles:
            await db.delete(task)
            deleted_dupes += 1
        else:
            seen_titles[key] = task

    await db.commit()

    remaining_tasks = await db.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled", "dropped"]))
    )
    remaining_blocks = await db.execute(
        select(AgendaBlock).where(AgendaBlock.task_id.isnot(None))
    )
    return {
        "status": "ok",
        "deleted_orphan_blocks": deleted_blocks,
        "deleted_duplicate_tasks": deleted_dupes,
        "remaining_active_tasks": len(remaining_tasks.scalars().all()),
        "remaining_task_blocks": len(remaining_blocks.scalars().all()),
    }


@router.post("/cleanup-orphan-blocks")
async def cleanup_orphan_blocks(db: AsyncSession = Depends(get_db)) -> dict:
    valid_result = await db.execute(
        select(Task.id).where(Task.status.notin_(["done", "cancelled", "dropped"]))
    )
    valid_ids = {row[0] for row in valid_result.all()}
    blocks_result = await db.execute(
        select(AgendaBlock).where(AgendaBlock.task_id.isnot(None))
    )
    deleted = 0
    for block in blocks_result.scalars().all():
        if block.task_id not in valid_ids:
            await db.delete(block)
            deleted += 1
    await db.commit()
    return {"status": "ok", "deleted": deleted}
