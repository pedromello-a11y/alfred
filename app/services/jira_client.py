"""
jira_client.py — Jira Cloud REST API v3
Funções: fetch_my_issues(), sync_to_cache()
"""
import base64
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _auth_headers() -> dict:
    token = base64.b64encode(
        f"{settings.jira_email}:{settings.jira_api_token}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


async def fetch_my_issues() -> list[dict]:
    """Retorna issues In Progress do Pedro via Jira Cloud REST API v3."""
    jql = 'assignee = currentUser() AND statusCategory = "In Progress" ORDER BY priority DESC'
    params = {
        "jql": jql,
        "fields": "summary,status,priority,duedate,project,description",
        "maxResults": 20,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{settings.jira_base_url}/rest/api/3/search",
                params=params,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            issues = []
            for issue in data.get("issues", []):
                f = issue["fields"]
                issues.append({
                    "key": issue["key"],
                    "summary": f["summary"],
                    "status": f["status"]["name"],
                    "priority": f["priority"]["name"] if f.get("priority") else None,
                    "deadline": f.get("duedate"),
                    "project": f["project"]["name"],
                })
            logger.info("Jira: {} issues fetched.", len(issues))
            return issues
    except Exception as exc:
        logger.error("fetch_my_issues failed: {}", exc)
        return []


async def sync_to_cache(db: AsyncSession) -> dict:
    """
    Sincroniza issues Jira com a tabela jira_cache.
    Retorna {"new": n, "updated": n, "total": n}.
    """
    from app.models import JiraCache

    issues = await fetch_my_issues()
    if not issues:
        return {"new": 0, "updated": 0, "total": 0}

    now = datetime.now(timezone.utc)
    new_count = updated_count = 0

    for issue in issues:
        result = await db.execute(
            select(JiraCache).where(JiraCache.jira_key == issue["key"])
        )
        cached = result.scalar_one_or_none()

        deadline = None
        if issue["deadline"]:
            try:
                deadline = datetime.fromisoformat(issue["deadline"])
            except ValueError:
                pass

        if cached is None:
            db.add(JiraCache(
                jira_key=issue["key"],
                summary=issue["summary"],
                status=issue["status"],
                priority=issue["priority"],
                deadline=deadline,
                project_name=issue["project"],
                last_synced=now,
            ))
            new_count += 1
            logger.info("Jira cache: new issue {}", issue["key"])
        else:
            status_changed = cached.status != issue["status"]
            cached.summary = issue["summary"]
            cached.status = issue["status"]
            cached.priority = issue["priority"]
            cached.deadline = deadline
            cached.project_name = issue["project"]
            cached.last_synced = now
            if status_changed:
                updated_count += 1
                logger.info("Jira cache: status changed {} → {}", issue["key"], issue["status"])

    await db.commit()
    return {"new": new_count, "updated": updated_count, "total": len(issues)}


async def get_cached_issues(db: AsyncSession) -> list:
    """Retorna issues do cache local, ordenadas por priority."""
    from app.models import JiraCache
    result = await db.execute(
        select(JiraCache).order_by(JiraCache.priority.nulls_last())
    )
    return result.scalars().all()
