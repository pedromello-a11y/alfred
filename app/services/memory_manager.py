"""
memory_manager.py — Consolidação hierárquica de memórias e export Obsidian.

Hierarquia:
  Diária (21h, nightly_closing) → Semanal (domingo 20h) → Mensal (dia 1, 20h)

Funções públicas:
  consolidate_daily(db)   — sintetiza mensagens + tarefas do dia via Haiku
  consolidate_weekly(db)  — consolida 7 diárias via Sonnet, marca superseded
  consolidate_monthly(db) — consolida 4 semanais via Sonnet, marca superseded
  export_to_obsidian()    — exporta memórias ativas para exports/ em Markdown
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import DailyPlan, Memory, Task
from app.services import brain

_EXPORTS_DIR = Path("exports")


# ---------------------------------------------------------------------------
# Consolidação diária
# ---------------------------------------------------------------------------

async def consolidate_daily(db: AsyncSession) -> Memory | None:
    """
    Sintetiza mensagens e tarefas do dia via Haiku.
    Chamado pelo nightly_closing; retorna o objeto Memory criado (ou None se já existir).
    """
    today = date.today()

    # Idempotência: não duplicar se já consolidou hoje
    existing = await db.execute(
        select(Memory)
        .where(Memory.memory_type == "daily")
        .where(Memory.period_start == today)
    )
    if existing.scalar_one_or_none():
        logger.info("Daily memory for {} already exists, skipping.", today)
        return None

    # Tarefas do dia
    today_start = datetime.combine(today, datetime.min.time())
    done_result = await db.execute(
        select(Task)
        .where(Task.status == "done")
        .where(Task.completed_at >= today_start)
    )
    done_today = done_result.scalars().all()

    created_result = await db.execute(
        select(Task).where(Task.created_at >= today_start)
    )
    created_today = created_result.scalars().all()

    tarefas_criadas = ", ".join(t.title for t in created_today) or "nenhuma"
    tarefas_concluidas = ", ".join(t.title for t in done_today) or "nenhuma"

    # Mensagens do dia (últimas 50)
    from app.models import Message
    msgs_result = await db.execute(
        select(Message)
        .where(Message.created_at >= today_start)
        .order_by(Message.created_at)
        .limit(50)
    )
    msgs = msgs_result.scalars().all()
    mensagens_txt = "\n".join(
        f"[{m.direction}] {m.content[:200]}" for m in msgs
    ) or "(sem mensagens registradas)"

    prompt = (
        "Resuma o dia do Pedro com base nos dados abaixo. "
        "Mantenha apenas informações que terão valor nos próximos dias ou semanas. "
        "Ignore conversa casual sem conteúdo operacional.\n\n"
        f"Mensagens do dia:\n{mensagens_txt}\n\n"
        f"Tarefas criadas: {tarefas_criadas}\n"
        f"Tarefas concluídas: {tarefas_concluidas}\n\n"
        "Formato: texto corrido, máx 300 palavras. Sem bullet points."
    )

    content = await brain.consolidate_memory("daily", prompt, db=db)

    memory = Memory(
        memory_type="daily",
        content=content,
        period_start=today,
        period_end=today,
    )
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    logger.info("Daily memory consolidated for {}.", today)
    return memory


# ---------------------------------------------------------------------------
# Consolidação semanal
# ---------------------------------------------------------------------------

async def consolidate_weekly(db: AsyncSession) -> Memory | None:
    """
    Consolida 7 memórias diárias via Sonnet.
    Antes de consolidar, verifica se todas têm consolidated=True em daily_plans.
    Se não, consolida os pendentes primeiro.
    Marca as 7 diárias como superseded=True.
    """
    today = date.today()
    week_start = today - timedelta(days=6)  # últimos 7 dias (domingo)

    # Idempotência
    existing = await db.execute(
        select(Memory)
        .where(Memory.memory_type == "weekly")
        .where(Memory.period_start == week_start)
    )
    if existing.scalar_one_or_none():
        logger.info("Weekly memory for week starting {} already exists.", week_start)
        return None

    # Retry: checar daily_plans não consolidados nos últimos 7 dias
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        plan_result = await db.execute(
            select(DailyPlan)
            .where(DailyPlan.plan_date == day)
            .where(DailyPlan.consolidated == False)  # noqa: E712
        )
        plan = plan_result.scalar_one_or_none()
        if plan:
            logger.info("Daily plan for {} not consolidated — running consolidate_daily.", day)
            await consolidate_daily(db)

    # Buscar memórias diárias dos últimos 7 dias
    daily_result = await db.execute(
        select(Memory)
        .where(Memory.memory_type == "daily")
        .where(Memory.period_start >= week_start)
        .where(Memory.period_start <= today)
        .where(Memory.superseded == False)  # noqa: E712
        .order_by(Memory.period_start)
    )
    dailies = daily_result.scalars().all()

    if not dailies:
        logger.warning("No daily memories found for weekly consolidation.")
        return None

    raw_data = "\n\n---\n\n".join(
        f"Dia {m.period_start.isoformat()}:\n{m.content}" for m in dailies
    )

    prompt = (
        "Consolide as memórias diárias da semana abaixo. "
        "Produza um resumo de 300-400 palavras com: principais entregas, "
        "padrões observados (dias produtivos, bloqueios recorrentes), "
        "decisões ou combinados importantes, itens ainda pendentes.\n\n"
        f"{raw_data}"
    )

    content = await brain.consolidate_memory("weekly", prompt, db=db)

    memory = Memory(
        memory_type="weekly",
        content=content,
        period_start=week_start,
        period_end=today,
    )
    db.add(memory)

    # Marcar diárias como superseded
    for m in dailies:
        m.superseded = True

    await db.commit()
    await db.refresh(memory)
    logger.info("Weekly memory consolidated: {} to {}.", week_start, today)
    return memory


# ---------------------------------------------------------------------------
# Consolidação mensal
# ---------------------------------------------------------------------------

async def consolidate_monthly(db: AsyncSession) -> Memory | None:
    """
    Consolida as 4 últimas memórias semanais via Sonnet.
    Marca as semanais como superseded=True.
    """
    today = date.today()
    month_start = today.replace(day=1)

    # Idempotência
    existing = await db.execute(
        select(Memory)
        .where(Memory.memory_type == "monthly")
        .where(Memory.period_start == month_start)
    )
    if existing.scalar_one_or_none():
        logger.info("Monthly memory for {} already exists.", month_start)
        return None

    weekly_result = await db.execute(
        select(Memory)
        .where(Memory.memory_type == "weekly")
        .where(Memory.superseded == False)  # noqa: E712
        .order_by(Memory.period_start.desc())
        .limit(4)
    )
    weeklies = weekly_result.scalars().all()

    if not weeklies:
        logger.warning("No weekly memories found for monthly consolidation.")
        return None

    raw_data = "\n\n---\n\n".join(
        f"Semana {m.period_start.isoformat()} a {m.period_end.isoformat()}:\n{m.content}"
        for m in weeklies
    )

    prompt = (
        "Consolide as memórias semanais do mês abaixo. "
        "Produza um resumo de 400-500 palavras com visão geral do mês: "
        "grandes entregas, tendências, aprendizados e próximos focos.\n\n"
        f"{raw_data}\n\n"
        "Ao final do resumo, adicione um bloco JSON separado por '---FACTS---' com 3-5 fatos estáveis "
        "sobre o Pedro. Formato exato:\n"
        "---FACTS---\n"
        "{\"facts\": [{\"key\": \"fact:nome\", \"value\": \"descrição\", \"operation\": \"ADD\"}]}\n"
        "Use operation 'UPDATE' se o fato provavelmente atualiza algo já conhecido. "
        "Exemplos de fatos: estilo de trabalho, horários produtivos, bloqueadores recorrentes, preferências."
    )

    raw_content = await brain.consolidate_memory("monthly", prompt, db=db)

    # Separar resumo dos fatos semânticos
    content = raw_content
    if "---FACTS---" in raw_content:
        parts = raw_content.split("---FACTS---", 1)
        content = parts[0].strip()
        facts_raw = parts[1].strip()
        await _save_semantic_facts(facts_raw, db)

    memory = Memory(
        memory_type="monthly",
        content=content,
        period_start=month_start,
        period_end=today,
    )
    db.add(memory)

    for m in weeklies:
        m.superseded = True

    await db.commit()
    await db.refresh(memory)
    logger.info("Monthly memory consolidated for {}.", month_start)
    return memory


async def _save_semantic_facts(facts_raw: str, db) -> None:
    """Parseia JSON de fatos semânticos e salva em settings com prefixo fact:."""
    from app.services import task_manager
    try:
        data = json.loads(facts_raw)
        facts = data.get("facts", [])
        for fact in facts:
            key = fact.get("key", "").strip()
            value = fact.get("value", "").strip()
            if key and value and key.startswith("fact:"):
                await task_manager.set_setting(key, value, db)
                logger.info("Semantic fact saved: {} = {}", key, value)
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        logger.warning("Failed to parse semantic facts: {}", exc)


# ---------------------------------------------------------------------------
# Export para Obsidian
# ---------------------------------------------------------------------------

async def export_to_obsidian() -> None:
    """
    Exporta memórias ativas para Markdown em exports/ com frontmatter YAML.
    Arquivos gerados:
      exports/memorias/YYYY-MM-DD-diaria.md
      exports/memorias/YYYY-MM-DD-semanal.md
      exports/dias/YYYY-MM-DD.md   (tarefas concluídas no dia)
    """
    _EXPORTS_DIR.mkdir(exist_ok=True)
    ((_EXPORTS_DIR / "memorias")).mkdir(exist_ok=True)
    ((_EXPORTS_DIR / "dias")).mkdir(exist_ok=True)

    async with AsyncSessionLocal() as db:
        # Memórias ativas (não superseded)
        result = await db.execute(
            select(Memory).where(Memory.superseded == False)  # noqa: E712
        )
        memories = result.scalars().all()

        for mem in memories:
            type_suffix = {
                "daily": "diaria",
                "weekly": "semanal",
                "monthly": "mensal",
            }.get(mem.memory_type, mem.memory_type)

            filename = _EXPORTS_DIR / "memorias" / f"{mem.period_start.isoformat()}-{type_suffix}.md"
            content = _render_memory_md(mem)
            filename.write_text(content, encoding="utf-8")
            logger.debug("Exported memory: {}", filename)

        # Dias: tarefas concluídas por dia (últimos 30 dias)
        cutoff = date.today() - timedelta(days=30)
        tasks_result = await db.execute(
            select(Task)
            .where(Task.status == "done")
            .where(Task.completed_at >= datetime.combine(cutoff, datetime.min.time()))
            .order_by(Task.completed_at)
        )
        tasks = tasks_result.scalars().all()

        # Agrupar por dia
        by_day: dict[date, list[Task]] = {}
        for t in tasks:
            d = t.completed_at.date() if t.completed_at else date.today()
            by_day.setdefault(d, []).append(t)

        for day, day_tasks in by_day.items():
            filename = _EXPORTS_DIR / "dias" / f"{day.isoformat()}.md"
            content = _render_day_md(day, day_tasks)
            filename.write_text(content, encoding="utf-8")
            logger.debug("Exported day: {}", filename)

    logger.info("Obsidian export complete. {} memories.", len(memories))


def _render_memory_md(mem: Memory) -> str:
    """Renderiza memória como Markdown com frontmatter YAML."""
    tags_map = {
        "daily": ["alfred", "diaria", "memoria"],
        "weekly": ["alfred", "semanal", "memoria"],
        "monthly": ["alfred", "mensal", "memoria"],
    }
    tags = tags_map.get(mem.memory_type, ["alfred", "memoria"])
    tags_str = "\n".join(f"  - {t}" for t in tags)

    return (
        f"---\n"
        f"type: {mem.memory_type}\n"
        f"period_start: {mem.period_start.isoformat()}\n"
        f"period_end: {mem.period_end.isoformat()}\n"
        f"created: {mem.created_at.isoformat() if mem.created_at else ''}\n"
        f"tags:\n{tags_str}\n"
        f"---\n\n"
        f"{mem.content}\n"
    )


def _render_day_md(day: date, tasks: list[Task]) -> str:
    """Renderiza dia com tarefas concluídas como Markdown."""
    lines = [
        f"---",
        f"date: {day.isoformat()}",
        f"tags:",
        f"  - alfred",
        f"  - dia",
        f"---",
        f"",
        f"# {day.strftime('%d/%m/%Y')}",
        f"",
        f"## Tarefas concluídas",
        f"",
    ]
    for t in tasks:
        cat = f" `{t.category}`" if t.category else ""
        pts = f" (+{t.estimated_minutes}min estimados)" if t.estimated_minutes else ""
        boss = " ⚔️" if t.is_boss_fight else ""
        lines.append(f"- [[{t.title}]]{cat}{pts}{boss}")

    lines += ["", f"Total: {len(tasks)} tarefa{'s' if len(tasks) != 1 else ''} concluída{'s' if len(tasks) != 1 else ''}."]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Relatório semanal
# ---------------------------------------------------------------------------

async def generate_weekly_report() -> str:
    """
    Gera relatório 'onde foi meu tempo' da semana:
    % por categoria, dia mais produtivo, fator de estimativa.
    Retorna texto formatado para WhatsApp.
    """
    async with AsyncSessionLocal() as db:
        today = date.today()
        week_start = today - timedelta(days=6)

        result = await db.execute(
            select(Task)
            .where(Task.status == "done")
            .where(Task.completed_at >= datetime.combine(week_start, datetime.min.time()))
        )
        tasks = result.scalars().all()

        if not tasks:
            return "Nenhuma tarefa concluída essa semana. Semana de recarga!"

        # Breakdown por categoria
        work_mins = sum(t.estimated_minutes or 30 for t in tasks if t.category == "work")
        personal_mins = sum(t.estimated_minutes or 30 for t in tasks if t.category == "personal")
        total_mins = work_mins + personal_mins or 1
        pct_work = round(work_mins / total_mins * 100)
        pct_personal = round(personal_mins / total_mins * 100)

        # Dia mais produtivo
        by_day: dict[date, int] = {}
        for t in tasks:
            d = t.completed_at.date() if t.completed_at else today
            by_day[d] = by_day.get(d, 0) + 1
        best_day = max(by_day, key=by_day.get)
        best_day_str = best_day.strftime("%A %d/%m")

        # Fator de estimativa (real/estimado)
        estimados = [t for t in tasks if t.estimated_minutes and t.actual_minutes]
        if estimados:
            fator = sum(t.actual_minutes / t.estimated_minutes for t in estimados) / len(estimados)
            fator_str = f"Você subestima em média {round((fator - 1) * 100)}%." if fator > 1.05 else "Estimativas estão precisas."
        else:
            fator_str = ""

        # Boss fights da semana
        boss_fights = [t for t in tasks if t.is_boss_fight]

        # XP total da semana (via PlayerStat — proxy: contar pontos das tarefas concluídas)
        from app.services import task_manager as _tm
        xp_this_week = sum(_tm.calculate_points(t) for t in tasks)

        # Tarefas semana anterior (para comparação)
        prev_start = week_start - timedelta(days=7)
        prev_result = await db.execute(
            select(Task)
            .where(Task.status == "done")
            .where(Task.completed_at >= datetime.combine(prev_start, datetime.min.time()))
            .where(Task.completed_at < datetime.combine(week_start, datetime.min.time()))
        )
        prev_tasks = prev_result.scalars().all()
        prev_xp = sum(_tm.calculate_points(t) for t in prev_tasks)

        comparison = ""
        xp_comparison = ""
        if prev_tasks:
            diff = len(tasks) - len(prev_tasks)
            sign = "+" if diff >= 0 else ""
            comparison = f" vs {len(prev_tasks)} semana passada ({sign}{diff})"
        if prev_xp:
            xp_diff = xp_this_week - prev_xp
            xp_sign = "+" if xp_diff >= 0 else ""
            xp_comparison = f" ({xp_sign}{xp_diff} vs semana passada)"

        lines = [
            f"📊 Semana {week_start.strftime('%d/%m')} - {today.strftime('%d/%m')}",
            f"",
            f"Tarefas concluídas: {len(tasks)}{comparison}",
            f"Distribuição: {pct_work}% work / {pct_personal}% pessoal",
            f"Dia mais produtivo: {best_day_str} ({by_day[best_day]} tarefas)",
            f"XP total: {xp_this_week}{xp_comparison}",
        ]
        if boss_fights:
            lines.append(f"Boss fights derrotados: {len(boss_fights)} ⚔️")
        if fator_str:
            lines.append(fator_str)

        return "\n".join(lines)
