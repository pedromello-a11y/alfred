"""Gera resumo do dia para WhatsApp."""
from datetime import datetime, timedelta, time
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services.time_utils import now_brt, to_brt_naive
from app.services.gcal_client import get_events_range
from app.services.block_engine import build_suggested_blocks


def _parse_title(title: str) -> tuple[str, str]:
    if " | " in (title or ""):
        p, t = title.split(" | ", 1)
        return p.strip(), t.strip()
    return "", (title or "").strip()


async def generate_morning_briefing(db: AsyncSession, start_time: datetime = None) -> str:
    """Gera mensagem de bom dia com resumo do dia."""
    hoje = now_brt().date()
    agora = start_time or now_brt()

    result = await db.execute(
        select(Task).where(
            and_(
                Task.status.in_(["pending", "in_progress"]),
                Task.category != "personal",
            )
        )
    )
    all_tasks = result.scalars().all()

    hoje_tasks = []
    for t in all_tasks:
        dl = t.deadline
        if dl:
            dl_date = dl.date() if hasattr(dl, "date") else dl
            if dl_date == hoje:
                hoje_tasks.append(t)

    # Reuniões do dia via GCal
    reunioes = []
    try:
        start_of_day = datetime.combine(hoje, time(0, 0))
        end_of_day = datetime.combine(hoje, time(23, 59))
        events = await get_events_range(start_of_day, end_of_day, db)
        for ev in (events or []):
            title = ev.get("summary", "Reunião")
            hora = ""
            start_ev = ev.get("start", {})
            if "dateTime" in start_ev:
                try:
                    dt = datetime.fromisoformat(start_ev["dateTime"])
                    hora = to_brt_naive(dt).strftime("%H:%M")
                except Exception:
                    pass
            reunioes.append(f"• {hora} {title}".strip())
    except Exception:
        pass

    # Blocos sugeridos para hoje
    blocos_texto = []
    try:
        suggested, _ = await build_suggested_blocks(db, hoje, hoje)
        for b in suggested[:6]:
            blocos_texto.append(f"• {b.get('start', '')}—{b.get('end', '')} → {b.get('title', '')}")
    except Exception:
        pass

    msg = f"Bom dia! ☀️ Expediente iniciado às {agora.strftime('%H:%M')}.\n"

    if hoje_tasks:
        msg += "\n📋 Prazo hoje:\n"
        for t in hoje_tasks:
            tipo = "🔴" if t.deadline_type == "hard" else "🟡"
            _, task_name = _parse_title(t.title)
            msg += f"• {tipo} {task_name}\n"

    if reunioes:
        msg += "\n📅 Reuniões:\n" + "\n".join(reunioes) + "\n"

    if blocos_texto:
        msg += "\n⏱️ Blocos sugeridos:\n" + "\n".join(blocos_texto) + "\n"

    msg += f"\n📊 {len(all_tasks)} demandas ativas esta semana.\nBora! 💪"
    return msg


async def generate_evening_summary(db: AsyncSession, end_time: datetime = None) -> str:
    """Gera mensagem de fim de dia com resumo."""
    agora = end_time or now_brt()
    hoje = agora.date()

    result = await db.execute(select(Task).where(Task.category != "personal"))
    all_tasks = result.scalars().all()

    concluidas_hoje = []
    pendentes = []
    amanha = hoje + timedelta(days=1)
    if amanha.weekday() >= 5:
        amanha = amanha + timedelta(days=(7 - amanha.weekday()))
    amanha_tasks = []

    for t in all_tasks:
        if t.status == "done":
            if t.completed_at:
                comp_date = t.completed_at.date() if hasattr(t.completed_at, "date") else t.completed_at
                if comp_date == hoje:
                    concluidas_hoje.append(t)
        elif t.status in ("pending", "in_progress"):
            pendentes.append(t)
            dl = t.deadline
            if dl:
                dl_date = dl.date() if hasattr(dl, "date") else dl
                if dl_date <= amanha:
                    amanha_tasks.append(t)

    msg = f"Bom descanso! 🌙 Expediente encerrado às {agora.strftime('%H:%M')}.\n"
    msg += "\n📊 Resumo do dia:\n"
    plural_c = "s" if len(concluidas_hoje) != 1 else ""
    plural_p = "s" if len(pendentes) != 1 else ""
    msg += f"• ✅ {len(concluidas_hoje)} task{plural_c} concluída{plural_c}\n"
    msg += f"• ⏳ {len(pendentes)} pendente{plural_p}\n"

    if amanha_tasks:
        msg += "\n📋 Amanhã:\n"
        for t in amanha_tasks[:5]:
            _, task_name = _parse_title(t.title)
            msg += f"• {task_name}\n"
    elif pendentes:
        msg += "\n✨ Nada urgente amanhã.\n"
    else:
        msg += "\n🎉 Sem demandas pendentes!\n"

    msg += "\nDescansa bem! 💤"
    return msg


async def generate_deadline_alert(task) -> str:
    """Gera alerta de deadline próximo."""
    _, task_name = _parse_title(task.title or "")
    dl = task.deadline
    dl_str = ""
    if dl and hasattr(dl, "strftime"):
        dl_str = dl.strftime("%H:%M")
    return f"⚠️ Lembrete: *{task_name}* vence hoje às {dl_str}.\n\nTá encaminhado? 💪"
