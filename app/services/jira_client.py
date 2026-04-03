"""Cliente Jira do Alfred — sync de issues para tasks locais."""
from base64 import b64encode
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import JiraCache, Task


def _auth_header() -> str:
    token = b64encode(
        f"{settings.jira_email}:{settings.jira_api_token}".encode()
    ).decode()
    return f"Basic {token}"


def _is_configured() -> bool:
    return bool(
        settings.jira_base_url and settings.jira_email and settings.jira_api_token
    )


async def fetch_my_issues() -> list[dict]:
    """Fetches open Jira issues assigned to the current user."""
    if not _is_configured():
        return []

    jql = (
        "assignee = currentUser() AND status NOT IN (Done, Closed, Resolved) "
        "ORDER BY priority ASC, updated DESC"
    )
    url = f"{settings.jira_base_url}/rest/api/3/search/jql"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url,
            headers={"Authorization": _auth_header(), "Accept": "application/json", "Content-Type": "application/json"},
            json={
                "jql": jql,
                "maxResults": 50,
                "fields": ["summary", "status", "priority", "duedate", "project", "description"],
            },
        )
        if resp.status_code != 200:
            logger.warning("Jira fetch failed: {} {}", resp.status_code, resp.text[:200])
            return []

        issues = []
        for issue in resp.json().get("issues", []):
            fields = issue.get("fields", {})
            issues.append({
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "status": (fields.get("status") or {}).get("name", ""),
                "priority": (fields.get("priority") or {}).get("name", ""),
                "duedate": fields.get("duedate"),
                "project": (fields.get("project") or {}).get("name", ""),
                "description": (fields.get("description") or "")[:500],
            })
        return issues


async def transition_issue(jira_key: str, target_status: str) -> dict:
    """Moves a Jira issue to a target status."""
    if not _is_configured():
        return {"status": "error", "error": "jira not configured"}

    url = f"{settings.jira_base_url}/rest/api/3/issue/{jira_key}/transitions"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            headers={"Authorization": _auth_header(), "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return {"status": "error", "error": f"fetch transitions failed: {resp.status_code}"}

        transitions = resp.json().get("transitions", [])
        target = next(
            (t for t in transitions if target_status.lower() in t["name"].lower()),
            None,
        )
        if not target:
            return {"status": "error", "error": f"transition to '{target_status}' not found"}

        resp2 = await client.post(
            url,
            headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
            json={"transition": {"id": target["id"]}},
        )
        if resp2.status_code not in (200, 204):
            return {"status": "error", "error": f"transition failed: {resp2.status_code}"}

        return {"status": "ok", "transition": target["name"]}


async def sync_issues_to_local(db: AsyncSession) -> int:
    """Syncs Jira issues to JiraCache and creates/updates local Tasks."""
    issues = await fetch_my_issues()
    if not issues:
        return 0

    now = datetime.now(timezone.utc)
    _priority_map = {"highest": 1, "high": 2, "medium": 3, "low": 4, "lowest": 5}
    synced = 0

    for issue in issues:
        deadline = None
        if issue.get("duedate"):
            try:
                deadline = datetime.fromisoformat(issue["duedate"])
            except ValueError:
                pass

        # Upsert JiraCache
        result = await db.execute(
            select(JiraCache).where(JiraCache.jira_key == issue["key"])
        )
        cache = result.scalar_one_or_none()
        if cache:
            cache.summary = issue["summary"]
            cache.status = issue["status"]
            cache.priority = issue["priority"]
            cache.deadline = deadline
            cache.project_name = issue["project"]
            cache.description_summary = issue["description"][:500]
            cache.last_synced = now
        else:
            cache = JiraCache(
                jira_key=issue["key"],
                summary=issue["summary"],
                status=issue["status"],
                priority=issue["priority"],
                deadline=deadline,
                project_name=issue["project"],
                description_summary=issue["description"][:500],
                last_synced=now,
            )
            db.add(cache)

        # Upsert local Task linked to this Jira key
        task_result = await db.execute(
            select(Task).where(Task.origin_ref == issue["key"]).limit(1)
        )
        task = task_result.scalar_one_or_none()

        title = (
            f"{issue['project']} | {issue['summary']}"
            if issue["project"]
            else issue["summary"]
        )
        priority = _priority_map.get((issue.get("priority") or "").lower(), 3)

        if task:
            task.title = title
            task.deadline = deadline
            task.priority = priority
        else:
            task = Task(
                title=title,
                origin="jira",
                origin_ref=issue["key"],
                status="pending",
                priority=priority,
                deadline=deadline,
                category="work",
            )
            db.add(task)

        synced += 1

    await db.commit()
    return synced


async def build_active_lines(db: AsyncSession) -> list[str]:
    """Returns formatted lines of open Jira issues for context building."""
    result = await db.execute(
        select(JiraCache)
        .where(JiraCache.status.notin_(["Done", "Closed", "Resolved"]))
        .order_by(JiraCache.deadline.nulls_last())
        .limit(10)
    )
    caches = result.scalars().all()
    lines = []
    for c in caches:
        prazo = c.deadline.strftime("%d/%m") if c.deadline else "sem prazo"
        lines.append(f"- [{c.jira_key}] {c.summary} ({c.status}, prazo {prazo})")
    return lines
