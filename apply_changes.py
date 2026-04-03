"""
Script único que aplica TODAS as mudanças nos 4 arquivos e faz git push.
Uso: python apply_changes.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# ═══════════════════════════════════════════════════════════════════════
# ARQUIVO 1: app/services/runtime_router.py — adicionar parser de datas
# e lógica de perguntar prazo
# ═══════════════════════════════════════════════════════════════════════

def patch_runtime_router():
    filepath = ROOT / "app" / "services" / "runtime_router.py"
    content = filepath.read_text(encoding="utf-8")

    # 1. Adicionar imports se não existem
    if "_re_mod" not in content:
        content = content.replace(
            "from datetime import datetime, time, timedelta",
            "import re as _re_mod\nfrom datetime import datetime, time, timedelta",
        )

    # 2. Adicionar bloco de funções novas ANTES de _compose_title
    NEW_FUNCTIONS = '''
# ── Parser de datas naturais PT-BR + fluxo de prazo ──────────────────
_NL_DATE_PATTERNS = [
    (_re_mod.compile(r"(?i)(\\d{1,2})/(\\d{1,2})(?:/(\\d{2,4}))?"), "dmy"),
    (_re_mod.compile(r"(?i)dia\\s+(\\d{1,2})"), "day_only"),
    (_re_mod.compile(r"(?i)amanh[aã]"), "tomorrow"),
    (_re_mod.compile(r"(?i)hoje"), "today"),
    (_re_mod.compile(r"(?i)segunda"), "wd_0"),
    (_re_mod.compile(r"(?i)ter[cç]a"), "wd_1"),
    (_re_mod.compile(r"(?i)quarta"), "wd_2"),
    (_re_mod.compile(r"(?i)quinta"), "wd_3"),
    (_re_mod.compile(r"(?i)sexta"), "wd_4"),
    (_re_mod.compile(r"(?i)s[aá]bado"), "wd_5"),
    (_re_mod.compile(r"(?i)domingo"), "wd_6"),
]
_NL_TIME_RE = _re_mod.compile(r"(?i)(?:às?|as|ate|até)\\s*(\\d{1,2})(?::(\\d{2}))?\\s*h?")
_NL_EOD_RE = _re_mod.compile(r"(?i)(fim do dia|final do dia|eod)")
_DIAS_SEMANA_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _parse_natural_date(text: str) -> datetime | None:
    """Parseia datas naturais em português. Retorna datetime naive BRT."""
    from app.services.time_utils import today_brt
    today = today_brt()
    target_date = None

    for pattern, kind in _NL_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if kind == "today":
            target_date = today
        elif kind == "tomorrow":
            target_date = today + timedelta(days=1)
        elif kind.startswith("wd_"):
            target_wd = int(kind.split("_")[1])
            days_ahead = target_wd - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target_date = today + timedelta(days=days_ahead)
        elif kind == "day_only":
            day_num = int(match.group(1))
            try:
                from datetime import date as _date_type
                target_date = today.replace(day=day_num)
                if target_date < today:
                    m = today.month + 1
                    y = today.year
                    if m > 12:
                        m, y = 1, y + 1
                    target_date = _date_type(y, m, day_num)
            except ValueError:
                continue
        elif kind == "dmy":
            day_num = int(match.group(1))
            month_num = int(match.group(2))
            yr = match.group(3)
            year_num = int(yr) if yr else today.year
            if year_num < 100:
                year_num += 2000
            try:
                from datetime import date as _date_type
                target_date = _date_type(year_num, month_num, day_num)
            except ValueError:
                continue
        if target_date:
            break

    if not target_date:
        return None

    hour, minute = 23, 59
    tm = _NL_TIME_RE.search(text)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2) or 0)
    elif _NL_EOD_RE.search(text):
        hour, minute = 23, 59

    return datetime(target_date.year, target_date.month, target_date.day, hour, minute)


async def _handle_deadline_response(raw_text: str, task_id_str: str, db: AsyncSession) -> tuple[str, bool]:
    """Tenta interpretar a mensagem como um prazo para a task pendente."""
    from uuid import UUID as _UUID

    parsed = _parse_natural_date(raw_text)
    if not parsed:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    try:
        task_uuid = _UUID(task_id_str)
    except ValueError:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    result = await db.execute(select(Task).where(Task.id == task_uuid))
    task = result.scalar_one_or_none()
    if not task:
        await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)
        return "", False

    task.deadline = parsed
    await db.commit()
    await db.refresh(task)
    await task_manager.set_setting("awaiting_deadline_for_task_id", "", db)

    dia = _DIAS_SEMANA_PT[parsed.weekday()]
    data_fmt = parsed.strftime("%d/%m/%Y")
    hora_fmt = parsed.strftime("%H:%M") if not (parsed.hour == 23 and parsed.minute == 59) else "fim do dia"

    lines = [
        f"\\u2705 Prazo definido: *{data_fmt}* ({dia})",
        "",
        f"*{task.title}*",
        f"Prazo: {data_fmt} — {hora_fmt}",
    ]
    hint = await _current_or_next_focus_hint(db)
    if hint:
        lines.append("")
        lines.append(hint)
    return "\\n".join(lines), True


'''

    if "_parse_natural_date" not in content:
        content = content.replace(
            "def _compose_title(",
            NEW_FUNCTIONS + "\ndef _compose_title(",
        )

    # 3. Substituir _build_new_task_response
    OLD_BUILD = '''async def _build_new_task_response(task: Task, db: AsyncSession) -> str:
    lines = [f"Anotado: *{task.title}*."]
    deadline = _format_deadline_brief(task.deadline)
    if deadline:
        lines.append(f"Prazo: {deadline}.")
    hint = await _current_or_next_focus_hint(db)
    if hint:
        lines.append(hint)
    return "\\n".join(lines)'''

    NEW_BUILD = '''async def _build_new_task_response(task: Task, db: AsyncSession) -> str:
    lines = [f"Anotado: *{task.title}*."]
    deadline = _format_deadline_brief(task.deadline)
    if deadline:
        lines.append(f"Prazo: {deadline}.")
        hint = await _current_or_next_focus_hint(db)
        if hint:
            lines.append(hint)
    else:
        await task_manager.set_setting("awaiting_deadline_for_task_id", str(task.id), db)
        lines.append("")
        lines.append("Qual o prazo de entrega? (ex: \\\\"dia 07\\\\", \\\\"sexta\\\\", \\\\"07/04\\\\")")
    return "\\n".join(lines)'''

    content = content.replace(OLD_BUILD, NEW_BUILD)

    # 4. Adicionar checagem de prazo no início de handle()
    HANDLE_HOOK = '''    if db is None:
        return await _legacy_fallback(raw_text, origin, db)

    # ── Checar se estamos aguardando prazo de task recém-criada ───────
    _awaiting_dl = await task_manager.get_setting("awaiting_deadline_for_task_id", db=db)
    if _awaiting_dl:
        _dl_response, _dl_handled = await _handle_deadline_response(raw_text, _awaiting_dl, db)
        if _dl_handled:
            return _make_item(origin, raw_text, "update", "deadline_set"), _dl_response, "deadline_set"
    # ── Fim check prazo ──────────────────────────────────────────────'''

    OLD_HANDLE = '''    if db is None:
        return await _legacy_fallback(raw_text, origin, db)

    decision = await interpreter.interpret_message(raw_text, db)'''

    NEW_HANDLE = HANDLE_HOOK + '''

    decision = await interpreter.interpret_message(raw_text, db)'''

    content = content.replace(OLD_HANDLE, NEW_HANDLE)

    filepath.write_text(content, encoding="utf-8")
    print(f"  ✅ {filepath.relative_to(ROOT)}")


# ═══════════════════════════════════════════════════════════════════════
# ARQUIVO 2: app/services/interpreter.py — adicionar regra de deadline
# ═══════════════════════════════════════════════════════════════════════

def patch_interpreter():
    filepath = ROOT / "app" / "services" / "interpreter.py"
    content = filepath.read_text(encoding="utf-8")

    OLD_RULE = "- Só preencha time_blocks quando a intenção principal for agenda_add."
    NEW_RULE = """- Se o usuário mencionar prazo ou deadline (ex: "até dia 07", "pra sexta", "até amanhã", "segunda até fim do dia", "entregar dia 10"), EXTRAIA a data em deadline_iso no formato ISO 8601 com timezone -03:00. "dia 07" = dia 07 do mês atual (ou próximo mês se já passou). "segunda"/"sexta" = próximo dia da semana. "amanhã" = dia seguinte. "fim do dia" = 23:59.
- Só preencha time_blocks quando a intenção principal for agenda_add."""

    if "Se o usuário mencionar prazo" not in content:
        content = content.replace(OLD_RULE, NEW_RULE)

    filepath.write_text(content, encoding="utf-8")
    print(f"  ✅ {filepath.relative_to(ROOT)}")


# ═══════════════════════════════════════════════════════════════════════
# ARQUIVO 3: app/routers/dashboard.py — week_offset + deadlines
# ═══════════════════════════════════════════════════════════════════════

def patch_dashboard():
    filepath = ROOT / "app" / "routers" / "dashboard.py"
    content = filepath.read_text(encoding="utf-8")

    # 1. Substituir _build_agenda_payload
    OLD_AGENDA = '''async def _build_agenda_payload(db: AsyncSession) -> list:
    """Returns week calendar grouped by day (0=Mon..4=Fri) for the frontend."""
    from datetime import timedelta
    from app.services.time_utils import today_brt
    today = today_brt()
    # Get Monday of current week
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    monday_dt = datetime.combine(monday, datetime.min.time())
    friday_dt = datetime.combine(friday, datetime.max.time().replace(microsecond=0))

    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= monday_dt)
        .where(AgendaBlock.start_at <= friday_dt)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = result.scalars().all()

    _type_map = {
        "meeting": "meeting",
        "break": "break",
        "focus": "focus",
        "personal": "personal",
        "admin": "meeting",
    }

    days: dict[int, list] = {i: [] for i in range(5)}
    for block in blocks:
        if not block.start_at:
            continue
        dow = block.start_at.weekday()
        if dow > 4:
            continue
        days[dow].append({
            "title": block.title,
            "time": block.start_at.strftime("%H:%M"),
            "end": block.end_at.strftime("%H:%M") if block.end_at else "",
            "type": _type_map.get(block.block_type or "focus", "focus"),
        })

    return [{"day": d, "events": events} for d, events in days.items()]'''

    NEW_AGENDA = '''async def _build_agenda_payload(db: AsyncSession, week_offset: int = 0) -> dict:
    """Returns week calendar with events and task deadlines."""
    from datetime import timedelta
    from app.services.time_utils import today_brt
    today = today_brt()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    friday = monday + timedelta(days=4)
    monday_dt = datetime.combine(monday, datetime.min.time())
    friday_dt = datetime.combine(friday, datetime.max.time().replace(microsecond=0))

    result = await db.execute(
        select(AgendaBlock)
        .where(AgendaBlock.start_at >= monday_dt)
        .where(AgendaBlock.start_at <= friday_dt)
        .where(AgendaBlock.status != "cancelled")
        .order_by(AgendaBlock.start_at.asc())
    )
    blocks = result.scalars().all()

    _type_map = {"meeting": "meeting", "break": "break", "focus": "focus", "personal": "personal", "admin": "meeting"}
    days: dict[int, list] = {i: [] for i in range(5)}
    for block in blocks:
        if not block.start_at:
            continue
        dow = block.start_at.weekday()
        if dow > 4:
            continue
        days[dow].append({
            "title": block.title,
            "time": block.start_at.strftime("%H:%M"),
            "end": block.end_at.strftime("%H:%M") if block.end_at else "",
            "type": _type_map.get(block.block_type or "focus", "focus"),
        })

    task_result = await db.execute(
        select(Task)
        .where(Task.status.in_(("pending", "in_progress")))
        .where(Task.deadline >= monday_dt)
        .where(Task.deadline <= friday_dt)
        .order_by(Task.deadline.asc())
    )
    deadlines = []
    for task in task_result.scalars().all():
        if not task.deadline:
            continue
        dow = task.deadline.weekday()
        if dow > 4:
            continue
        project, task_name = "", task.title or ""
        if "|" in task_name:
            parts = task_name.split("|", 1)
            project, task_name = parts[0].strip(), parts[1].strip()
        deadlines.append({
            "task_id": str(task.id),
            "title": task.title,
            "project": project,
            "taskName": task_name,
            "deadline": task.deadline.isoformat(),
            "day": dow,
            "time": task.deadline.strftime("%H:%M"),
            "is_overdue": task.deadline.date() < today,
            "is_today": task.deadline.date() == today,
            "status": task.status,
        })

    return {
        "days": [{"day": d, "events": events} for d, events in days.items()],
        "deadlines": deadlines,
        "weekStart": monday.isoformat(),
        "weekEnd": friday.isoformat(),
    }'''

    content = content.replace(OLD_AGENDA, NEW_AGENDA)

    # 2. Substituir dashboard_state
    OLD_STATE_HEADER = '''@router.get("/state")
async def dashboard_state(db: AsyncSession = Depends(get_db)) -> dict:'''

    NEW_STATE_HEADER = '''@router.get("/state")
async def dashboard_state(
    db: AsyncSession = Depends(get_db),
    week_offset: int = 0,
) -> dict:'''

    content = content.replace(OLD_STATE_HEADER, NEW_STATE_HEADER)

    # 3. Substituir chamada _build_agenda_payload
    content = content.replace(
        '"agenda": await _build_agenda_payload(db),',
        '''_agenda_data = await _build_agenda_payload(db, week_offset)
        # unpack agenda data below'''
    )

    # Adicionar novos campos e substituir agenda
    OLD_DUMP = '''        "dumpLibrary": await _build_dump_library(db),'''
    NEW_DUMP = '''        "agenda": _agenda_data.get("days", []),
        "agendaDeadlines": _agenda_data.get("deadlines", []),
        "agendaWeekStart": _agenda_data.get("weekStart", ""),
        "agendaWeekEnd": _agenda_data.get("weekEnd", ""),
        "dumpLibrary": await _build_dump_library(db),'''

    # Need a cleaner approach — let me just do find/replace on the return dict
    # Remove the old agenda line and add new ones
    if "_agenda_data" in content and '"agenda": _agenda_data' not in content:
        # Remove the "unpack agenda data below" comment line
        content = content.replace(
            '''        # unpack agenda data below
        "dumpLibrary": await _build_dump_library(db),''',
            NEW_DUMP
        )

    filepath.write_text(content, encoding="utf-8")
    print(f"  ✅ {filepath.relative_to(ROOT)}")


# ═══════════════════════════════════════════════════════════════════════
# ARQUIVO 4: alfred-dashboard.html — navegação + deadlines
# ═══════════════════════════════════════════════════════════════════════

def patch_dashboard_html():
    filepath = ROOT / "alfred-dashboard.html"
    content = filepath.read_text(encoding="utf-8")

    # 1. Adicionar CSS
    NEW_CSS = """.week-nav{display:flex;align-items:center;gap:8px;padding:4px 10px;flex-shrink:0;background:var(--s1);border-bottom:1px solid var(--b1)}.week-nav-btn{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:4px 10px;border-radius:4px;cursor:pointer;border:.5px solid var(--b2);background:transparent;color:var(--t-muted);transition:all .15s}.week-nav-btn:hover{color:var(--pur);border-color:var(--pur-brd)}.week-nav-label{font-family:'DM Mono',monospace;font-size:10px;color:var(--t-dim);flex:1;text-align:center;letter-spacing:.06em}.ev-deadline{background:var(--redbg);color:var(--red);border-left:2px solid var(--red);font-size:10px;position:absolute;left:2px;right:2px;border-radius:4px;padding:2px 6px;overflow:hidden}.ev-deadline.overdue{animation:bk 2s ease-in-out infinite}.ev-deadline.today{background:var(--goldbg);color:var(--gold);border-left-color:var(--gold)}.ev-deadline .ev-title{font-size:10px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.2}.ev-deadline .ev-time{font-family:'DM Mono',monospace;font-size:8px;opacity:.7}.task-deadline-badge{font-family:'DM Mono',monospace;font-size:9px;color:var(--gold);margin-top:2px}.task-deadline-badge.overdue{color:var(--red)}"""

    if ".week-nav{" not in content:
        content = content.replace("</style>", NEW_CSS + "\n</style>")

    # 2. Adicionar barra de navegação
    OLD_AGENDA_HTML = '''<div class="agenda">
    <div class="agenda-header">'''

    NEW_AGENDA_HTML = '''<div class="agenda">
    <div class="week-nav"><div class="week-nav-btn" onclick="changeWeek(-1)">← anterior</div><div class="week-nav-label" id="week-label">semana atual</div><div class="week-nav-btn" onclick="changeWeek(0)">hoje</div><div class="week-nav-btn" onclick="changeWeek(1)">próxima →</div></div>
    <div class="agenda-header">'''

    if "week-nav" not in content:
        content = content.replace(OLD_AGENDA_HTML, NEW_AGENDA_HTML)

    # 3. Substituir JavaScript INTEIRO
    OLD_SCRIPT_START = "<script>"
    OLD_SCRIPT_END = "</script>"

    script_start = content.index(OLD_SCRIPT_START)
    script_end = content.index(OLD_SCRIPT_END) + len(OLD_SCRIPT_END)

    NEW_SCRIPT = """<script>
var API_BASE=window.location.hostname==='localhost'||window.location.protocol==='file:'?'http://localhost:8000':'';
var openPanel=null,openDump=null,currentProjects=[],currentWeekOffset=0,currentDeadlines=[];
function splitCombinedTitle(v){var text=(v||'').trim();if(text.indexOf('|')>-1){var p=text.split('|');if(p.length>=2){var left=p.shift().trim(),right=p.join('|').trim();if(left&&right)return{project:left,task:right};}}return{project:'',task:text};}
async function postJson(url,body){var resp=await fetch(API_BASE+url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!resp.ok)throw new Error('HTTP '+resp.status);return await resp.json();}
async function sendToAlfred(id,action,note,date){return await postJson('/dashboard/action',{task_id:id,action:action,note:note||null,date:date||null});}
function showFeedback(id,text){var fb=document.getElementById('fb-'+id);if(fb){fb.textContent=text;fb.style.display='block';}}
function buildPanel(item,panelEl){var split=splitCombinedTitle(item.name||'');var today=(item.rawDate||'').split('T')[0]||new Date().toISOString().split('T')[0];panelEl.innerHTML='<input class="ctl" id="project-'+item.id+'" list="projects-list" value="'+(item.project||split.project||'')+'" placeholder="projeto"><input class="ctl" id="title-'+item.id+'" value="'+(item.taskName||split.task||'')+'" placeholder="nome da task"><input type="date" class="panel-date" value="'+today+'" id="date-'+item.id+'"><textarea class="panel-textarea" rows="2" placeholder="anotação rápida…" id="note-'+item.id+'"></textarea><div class="panel-actions"><div class="btn btn-send" onclick="saveTask(\\''+item.id+'\\')">salvar</div><div class="btn btn-done" onclick="completeTask(\\''+item.id+'\\')">✓ concluir</div><div class="btn btn-del" onclick="deleteTask(\\''+item.id+'\\')">⌫ excluir</div><div class="btn btn-cancel" onclick="closePanel()">✕</div></div><div class="panel-feedback" id="fb-'+item.id+'"></div>';}
async function saveTask(id){try{var title=(document.getElementById('title-'+id)||{}).value||'';var project=(document.getElementById('project-'+id)||{}).value||'';var date=(document.getElementById('date-'+id)||{}).value;var note=(document.getElementById('note-'+id)||{}).value||'';await postJson('/dashboard/task-edit',{task_id:id,title:title,project:project,date:date||'',note:note||null});showFeedback(id,'salvo ✓');await loadState();}catch(e){console.error(e);showFeedback(id,'erro ao salvar');}}
async function completeTask(id){try{var note=(document.getElementById('note-'+id)||{}).value||'';await sendToAlfred(id,'concluida',note||null,null);showFeedback(id,'concluída ✓');await loadState();closePanel();}catch(e){console.error(e);showFeedback(id,'erro ao concluir');}}
async function deleteTask(id){try{await sendToAlfred(id,'excluir',null,null);await loadState();closePanel();}catch(e){console.error(e);showFeedback(id,'erro ao excluir');}}
async function createTask(){try{var project=(document.getElementById('new-project')||{}).value||'';var title=(document.getElementById('new-title')||{}).value||'';var date=(document.getElementById('new-date')||{}).value||'';if(!title.trim())return;await postJson('/dashboard/create-task',{project:project||null,title:title,date:date||null});document.getElementById('new-title').value='';await loadState();}catch(e){console.error('create task failed',e);}}
function closePanel(){if(openPanel){openPanel.classList.remove('open');openPanel=null;}}
function togglePanel(item,panelEl){if(item.id==='__none')return;if(openPanel&&openPanel!==panelEl){openPanel.classList.remove('open');openPanel=null;}if(panelEl.classList.contains('open')){panelEl.classList.remove('open');openPanel=null;}else{buildPanel(item,panelEl);panelEl.classList.add('open');openPanel=panelEl;}}
function _dlBadge(rd){if(!rd)return'';var d=new Date(rd),now=new Date(),diff=Math.ceil((d-now)/864e5),t='',c='task-deadline-badge';if(diff<0){t='⚠️ atrasada';c+=' overdue';}else if(diff===0)t='📅 hoje';else if(diff===1)t='📅 amanhã';else{var dd=d.getDate().toString().padStart(2,'0'),mm=(d.getMonth()+1).toString().padStart(2,'0'),ds=['dom','seg','ter','qua','qui','sex','sáb'];t='📅 '+dd+'/'+mm+' ('+ds[d.getDay()]+')';}return t?'<span class="'+c+'">'+t+'</span>':'';}
function renderList(id,items){var c=document.getElementById(id);if(!c)return;c.innerHTML='';items.forEach(function(item){var wrap=document.createElement('div');wrap.className='task-item';var row=document.createElement('div');row.className='task-row';row.innerHTML='<span class="tdot '+item.dot+'"></span><span class="tcol">'+(item.project?'<span class="tproj">'+item.project+'</span>':'')+'<span class="tname '+item.cls+'">'+(item.taskName||item.name)+'</span>'+_dlBadge(item.rawDate)+'</span>'+(item.badge?'<span class="tbdg '+item.bdgcls+'">'+item.badge+'</span>':'');var panel=document.createElement('div');panel.className='task-panel';row.addEventListener('click',function(){togglePanel(item,panel);});wrap.appendChild(row);wrap.appendChild(panel);c.appendChild(wrap);});if(!items.length)c.innerHTML='<div class="task-item"><div class="task-row"><span class="tdot lo"></span><span class="tcol"><span class="tname">nenhum item</span></span></div></div>';}
function renderCategories(id,categories){var c=document.getElementById(id);if(!c)return;c.
innerHTML='';(categories||[]).forEach(function(cat){var el=document.createElement('div');el.className='cat-chip';el.textContent=cat.name+' · '+cat.count;c.appendChild(el);});if(!(categories||[]).length)c.innerHTML='<div class="cat-chip">sem dumps</div>';}
function renderDumpList(id,items){var c=document.getElementById(id);if(!c)return;c.innerHTML='';(items||[]).forEach(function(item){var card=document.createElement('div');card.className='dump-card';var confidence=Math.round((item.confidence||0)*100);card.innerHTML='<div class="dump-top"><div class="tdot '+((item.status==='unknown'||confidence<50)?'lo':'hi')+'"></div><div style="flex:1"><div class="dump-title">'+item.title+'</div><div class="dump-meta">'+(item.category||'desconhecido')+' · '+confidence+'% · '+(item.status||'unknown')+'</div></div></div><div class="dump-summary">'+(item.summary||'')+'</div><div class="dump-raw">'+(item.rawText||'')+'</div>';card.addEventListener('click',function(){if(openDump&&openDump!==card)openDump.classList.remove('open');card.classList.toggle('open');openDump=card.classList.contains('open')?card:null;});c.appendChild(card);});if(!(items||[]).length)c.innerHTML='<div class="dump-card"><div class="dump-title">nenhum item</div></div>';}
function fillProjects(projects){currentProjects=projects||[];var data=document.getElementById('projects-list');if(!data)return;data.innerHTML='';currentProjects.forEach(function(name){var opt=document.createElement('option');opt.value=name;data.appendChild(opt);});}
var CAL_START=9,CAL_END=19,ROW_H=56,HOURS=[];for(var h=CAL_START;h<=CAL_END;h++)HOURS.push(h);var N_HOURS=CAL_END-CAL_START,DAY_NAMES=['seg','ter','qua','qui','sex'],MONTHS=['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'],DAYS_PT=['dom','seg','ter','qua','qui','sex','sáb'],EVENTS={};
function timeToPx(t){var p=(t||'00:00').split(':');return(parseInt(p[0])+parseInt(p[1]||'0')/60-CAL_START)*ROW_H}
function getMonday(d){var m=new Date(d);m.setDate(d.getDate()-(d.getDay()===0?6:d.getDay()-1));return m}
function changeWeek(delta){if(delta===0)currentWeekOffset=0;else currentWeekOffset+=delta;loadState();}
function updateWeekLabel(ws,we){var lbl=document.getElementById('week-label');if(!lbl)return;if(currentWeekOffset===0){lbl.textContent='semana atual';}else if(ws&&we){var s=new Date(ws),e=new Date(we);lbl.textContent=s.getDate().toString().padStart(2,'0')+'/'+(s.getMonth()+1).toString().padStart(2,'0')+' — '+e.getDate().toString().padStart(2,'0')+'/'+(e.getMonth()+1).toString().padStart(2,'0');}else{lbl.textContent='semana '+(currentWeekOffset>0?'+':'')+currentWeekOffset;}}
function buildCalendar(weekStartISO){var now=new Date(),mon;if(weekStartISO){mon=new Date(weekStartISO);}else{mon=getMonday(now);mon=new Date(mon.getFullYear(),mon.getMonth(),mon.getDate()+currentWeekOffset*7);}var todayStr=now.getFullYear()+'-'+(now.getMonth()+1).toString().padStart(2,'0')+'-'+now.getDate().toString().padStart(2,'0');var totalH=N_HOURS*ROW_H;for(var i=0;i<5;i++){var d=new Date(mon);d.setDate(mon.getDate()+i);var dStr=d.getFullYear()+'-'+(d.getMonth()+1).toString().padStart(2,'0')+'-'+d.getDate().toString().padStart(2,'0');var isT=(dStr===todayStr);var dh=document.getElementById('dh'+i);dh.innerHTML='<div class="dh-name'+(isT?' td':'')+'">'+DAY_NAMES[i]+'</div><div class="dh-num'+(isT?' td':'')+'">'+d.getDate()+'</div>';}var inner=document.getElementById('cal-inner');inner.style.height=totalH+'px';inner.style.position='relative';var gutter=document.createElement('div');gutter.className='time-gutter';gutter.style.cssText='grid-column:1;position:relative;';HOURS.forEach(function(hr,idx){var lbl=document.createElement('div');lbl.className='time-label'+(idx===0?' first':'');lbl.style.top=(idx*ROW_H)+'px';lbl.textContent=hr+'h';gutter.appendChild(lbl);});inner.appendChild(gutter);for(var di=0;di<5;di++){var col=document.createElement('div');col.className='day-col';col.style.cssText='grid-column:'+(di+2)+';position:relative;';for(var li=0;li<=N_HOURS;li++){var line=document.createElement('div');line.className='hour-line';line.style.top=(li*ROW_H)+'px';col.appendChild(line);}(EVENTS[di]||[]).forEach(function(ev){var top=timeToPx(ev.time),ht=Math.max(timeToPx(ev.end)-top-2,18);var el=document.createElement('div');el.className='ev '+ev.type;el.style.cssText='top:'+top+'px;height:'+ht+'px;';el.innerHTML='<div class="ev-time">'+ev.time+'</div><div class="ev-title">'+ev.title+'</div>';col.appendChild(el);});(currentDeadlines||[]).forEach(function(dl){if(dl.day!==di)return;var dlTime=dl.time||'23:59';var dlH=parseInt(dlTime.split(':')[0]);var dispTime=dlTime;if(dlH<CAL_START)dispTime=CAL_START+':00';if(dlH>=CAL_END)dispTime=(CAL_END-1)+':30';var top=timeToPx(dispTime);var cls='ev-deadline';if(dl.is_overdue)cls+=' overdue';else if(dl.is_today)cls+=' today';var el=document.createElement('div');el.className=cls;el.style.cssText='top:'+top+'px;height:22px;';var icon=dl.is_overdue?'⚠️':dl.is_today?'🔶':'📅';el.innerHTML='<div class="ev-time">'+icon+' DEADLINE</div><div class="ev-title">'+dl.title+'</div>';col.appendChild(el);});inner.appendChild(col);}if(currentWeekOffset===0){var nowH=now.getHours()+now.getMinutes()/60;if(nowH>=CAL_START&&nowH<=CAL_END){var nowPx=timeToPx(now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0'));var nl=document.createElement('div');nl.className='now-line';nl.style.cssText='top:'+nowPx+'px;left:var(--time-w);right:0;';nl.innerHTML='<div class="now-dot"></div>';inner.appendChild(nl);}}document.querySelector('.cal-scroll').scrollTop=Math.max(0,((currentWeekOffset===0?now.getHours():9)-CAL_START-1)*ROW_H);}
function updateClock(){var n=new Date();document.getElementById('clock').textContent=n.getHours().toString().padStart(2,'0')+':'+n.getMinutes().toString().padStart(2,'0');document.getElementById('dateval').textContent=DAYS_PT[n.getDay()]+' · '+n.getDate()+' '+MONTHS[n.getMonth()];}
function sw(tab,el){el.parentElement.querySelectorAll('.stab').forEach(function(t){t.classList.remove('active')});el.classList.add('active');document.getElementById('pane-fila').classList.toggle('active',tab==='fila');document.getElementById('pane-horizonte').classList.toggle('active',tab==='horizonte');document.getElementById('pane-dumps').classList.toggle('active',tab==='dumps');}
function toRenderItem(t,idx){var split=splitCombinedTitle(t.title||t.name||'—');var priority=t.priority||'md';var bdgcls=priority==='hi'?'bdg-a':priority==='lo'?'bdg-m':'bdg-m';return{id:t.id,name:t.title||t.name||'—',taskName:t.taskName||split.task,project:t.project||split.project,rawDate:t.rawDate||t.deadlineRaw||'',cls:idx===0?'cur':(priority==='hi'?'hi':''),badge:t.priorityLabel||t.badge||'',bdgcls:bdgcls,dot:priority};}
function applyState(s){var fb=s.focusBoard||{},hb=s.horizonBoard||{},aq=s.activeQueue||[],dl=s.dumpLibrary||{},op=s.operational||{};fillProjects(s.projects||[]);var todayTasks=(fb.todayTasks||[]).map(function(t){var item=toRenderItem(t,0);return item;}),queueItems=(aq||[]).map(toRenderItem),tomorrow=(hb.tomorrow||[]).map(toRenderItem),week=(hb.thisWeek||[]).map(toRenderItem),later=(hb.later||[]).map(toRenderItem),priorityItems=(op.priorityTask?[toRenderItem(op.priorityTask,0)]:[]),overdueItems=(op.overdueTasks||[]).map(toRenderItem);var currentBlock=fb.currentBlock||null,nextBlock=fb.nextBlock||null;var focusTitle=(currentBlock&&currentBlock.title)||(todayTasks[0]&&todayTasks[0].name)||s.focus.title||'nenhuma tarefa ativa';var parts=splitCombinedTitle(focusTitle);document.getElementById('focus-project').textContent=(parts.project||'FOCO');document.getElementById('focus-task').textContent=(parts.task||focusTitle);var meta=[];if(currentBlock&&currentBlock.start)meta.push(currentBlock.start+' → '+currentBlock.end);if(fb.todayTasks&&fb.todayTasks[0]&&fb.todayTasks[0].estimate)meta.push(fb.todayTasks[0].estimate);if(fb.todayTasks&&fb.todayTasks[0]&&fb.todayTasks[0].deadline)meta.push('prazo: '+fb.todayTasks[0].deadline);document.getElementById('focus-meta').textContent=meta.join(' · ')||'sem bloco atual';document.getElementById('focus-badge').textContent=(fb.alerts&&fb.alerts[0])||'agora / próximo / horizonte';document.getElementById('next-name').textContent=(nextBlock&&nextBlock.title)||(hb.tomorrow&&hb.tomorrow[0]&&hb.tomorrow[0].title)||s.next.title||'—';document.getElementById('next-note').textContent=(nextBlock&&nextBlock.start?nextBlock.start+' → '+nextBlock.end:'')||s.next.note||'';document.getElementById('hint-suggestion-title').textContent=((op.suggestion||{}).project?((op.suggestion||{}).project+' | '):'')+(((op.suggestion||{}).title)||'—');document.getElementById('hint-suggestion-note').textContent=((op.suggestion||{}).reason)||'—';document.getElementById('hint-priority-title').textContent=(priorityItems[0]?((priorityItems[0].project?priorityItems[0].project+' | ':'')+(priorityItems[0].taskName||priorityItems[0].name)):'—');document.getElementById('hint-priority-note').textContent=(priorityItems[0]&&priorityItems[0].rawDate?priorityItems[0].rawDate.split('T')[0]:'sem prazo');document.getElementById('signal-now-title').textContent=(op.nowLabel||'—');document.getElementById('signal-now-meta').textContent=((op.suggestion||{}).reason)||'—';renderList('list-priority',priorityItems);renderList('list-overdue',overdueItems);renderList('list-hoje',todayTasks);renderList('list-fila',queueItems);renderList('list-amanha',tomorrow);renderList('list-semana',week);renderList('list-depois',later);renderCategories('dump-categories',dl.categories||[]);renderDumpList('dump-review',dl.needsReview||[]);renderDumpList('dump-recent',dl.recent||[]);EVENTS={};(s.agenda||[]).forEach(function(d){EVENTS[d.day]=(d.events||[]).map(function(e){var typeMap={meeting:'ev-m',deadline:'ev-d',focus:'ev-f',break:'ev-p',personal:'ev-p'};return{title:e.title,time:e.time,end:e.end,type:typeMap[e.type]||'ev-p'};});});currentDeadlines=s.agendaDeadlines||[];updateWeekLabel(s.agendaWeekStart,s.agendaWeekEnd);var xp=s.xp||{};document.getElementById('xp-lbl').textContent='nível '+(xp.level||1)+' · '+(xp.current||0)+'xp';document.getElementById('xp-pct').textContent=(xp.percent||0)+'%';document.getElementById('xp-fill').style.width=(xp.percent||0)+'%';var streak=xp.streak||0;document.getElementById('streak-lbl').textContent=streak+' dias';var dots=document.getElementById('sdots');dots.innerHTML='';for(var i=0;i<7;i++){var sp=document.createElement('span');sp.className='sd '+(i<streak?'on':'off');dots.appendChild(sp);}}
async function loadState(){try{var url=API_BASE+'/dashboard/state';if(currentWeekOffset!==0)url+='?week_offset='+currentWeekOffset;var resp=await fetch(url);if(!resp.ok)throw new Error('HTTP '+resp.status);var data=await resp.json();applyState(data);rebuildCalendar(data.agendaWeekStart);}catch(e){console.error('Alfred state fetch failed:',e);}}
function rebuildCalendar(weekStartISO){var inner=document.getElementById('cal-inner');inner.innerHTML='';buildCalendar(weekStartISO);}
buildCalendar();updateClock();setInterval(updateClock,15000);loadState();setInterval(loadState,60000);
</script>"""

    content = content[:script_start] + NEW_SCRIPT + content[script_end:]

    filepath.write_text(content, encoding="utf-8")
    print(f"  ✅ {filepath.relative_to(ROOT)}")


# ═══════════════════════════════════════════════════════════════════════
# GIT: commit + push
# ═══════════════════════════════════════════════════════════════════════

def git_push():
    print("\n📦 Git commit + push...")
    try:
        subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m",
             "feat: bot pergunta prazo + navegação semana dashboard + deadlines no calendário"],
            cwd=ROOT, check=True,
        )
        subprocess.run(["git", "push"], cwd=ROOT, check=True)
        print("  ✅ Push feito! Railway vai deployar automaticamente.")
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Erro no git: {e}")
        print("  Tente manualmente: git add -A && git commit -m 'feat: updates' && git push")


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    print("🔧 Aplicando mudanças no Alfred...\n")

    print("1/4 Patch runtime_router.py (parser datas + perguntar prazo)")
    patch_runtime_router()

    print("2/4 Patch interpreter.py (regra de deadline no prompt)")
    patch_interpreter()

    print("3/4 Patch dashboard.py (week_offset + deadlines)")
    patch_dashboard()

    print("4/4 Patch alfred-dashboard.html (navegação + deadlines visuais)")
    patch_dashboard_html()

    print("\n✅ Todos os patches aplicados!")

    resp = input("\nFazer git commit + push agora? (s/n): ").strip().lower()
    if resp in ("s", "sim", "y", "yes", ""):
        git_push()
    else:
        print("Ok. Quando quiser, rode: git add -A && git commit -m 'feat: updates' && git push")

    print("\n🧹 Para limpar dumps antigos, execute:")
    print('curl -X POST https://web-production-62584.up.railway.app/admin/dumps/clear-all -H "X-Admin-Key: alfred-admin-2026"')


if __name__ == "__main__":
    main()