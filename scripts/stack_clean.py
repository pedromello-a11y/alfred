import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Task

BAD = [
    re.compile(r"(?i)^nao era pra"),
    re.compile(r"(?i)^não era pra"),
    re.compile(r"(?i)^adicionar tarefa"),
    re.compile(r"(?i)^esse e um resumo"),
    re.compile(r"(?i)^esse é um resumo"),
]


def bad(title: str) -> bool:
    text = (title or "").strip()
    return (not text) or len(text) > 180 or any(p.search(text) for p in BAD)


async def main():
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Task).where(Task.status.in_(("pending", "in_progress"))))
        tasks = list(result.scalars().all())
        count = 0
        for task in tasks:
            if bad(task.title or ""):
                task.status = "cancelled"
                count += 1
        await db.commit()
        print({"checked": len(tasks), "cancelled": count})


if __name__ == "__main__":
    asyncio.run(main())
