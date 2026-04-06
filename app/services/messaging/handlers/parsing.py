"""
Funções de parsing de chunks de texto para extração de status, títulos,
notas e estimativas. Usadas por context_updates.py.
Sem dependências de outros handlers — apenas task_manager e regex.
"""
import re
from difflib import SequenceMatcher

from app.services import task_manager

# ── Padrões (copiados de message_handler para não criar import circular) ──────

_STATUS_PATTERNS = {
    "done": re.compile(r"(?i)(terminei|finalizei|concluí|conclui|entreguei|foi entregue|foi aprovado|resolvido|resolvida|concluído|concluída|concluida)"),
    "done_external": re.compile(r"(?i)(rig já fez|rig ja fez|rig já entregou|rig ja entregou|já entregou|ja entregou)"),
    "in_progress": re.compile(r"(?i)(em andamento|ativo agora|ativa agora|ativo|frente estratégica ativa|frente strategica ativa|startado|startei|comecei|iniciei|segue|continua|mandei briefing|briefing enviado|assets prontos|assets chegaram|está andando|esta andando|está rolando|esta rolando|rolando)"),
    "pending": re.compile(r"(?i)(pendente|em aberto|aberto|registrado|registrada|próximo da fila|proximo da fila|secundário|secundario|travado|pausou|voltou para pendente)"),
}

_NEGATED_DONE = re.compile(r"(?i)(ainda não terminei|ainda nao terminei|não terminei|nao terminei|não conclu[ií]|nao conclu[ií]|não entreguei|nao entreguei|ainda falta)")
_NEGATED_NOT_STARTED = re.compile(r"(?i)(ainda não comecei|ainda nao comecei|não comecei|nao comecei)")
_NEGATED_PENDING_TO_DONE = re.compile(r"(?i)(não está pendente|nao esta pendente|não está mais ativo|nao esta mais ativo|não está ativo|nao esta ativo)")
_NOTE_ONLY_HINTS = re.compile(r"(?i)(briefing|keyframes?|reuni[aã]o|3k|alinhar|alinhamento|assets prontos|assets chegaram|storyboard)")
_SYSTEM_HINTS = re.compile(r"(?i)(áudio|audio|bug do áudio|bug do audio|ajustes do sistema|sistema alfred|alfred continua quebrado)")
_FIELD_LINE_PREFIXES = re.compile(r"(?i)^(status|estimativa|estimativa que você me passou|estimativa que voce me passou|prioridade|briefing|cronograma|falta|função|funcao|checar|assets|ideia atual|preocupação principal|preocupacao principal)\s*:")
_SKIP_UPDATE_CHUNKS = re.compile(r"(?i)^(demandas ativas agora|outras demandas novas|itens já resolvidos|itens de radar|galaxy|spark|cast|detalhe)$")
_IGNORE_CONTEXT_CHUNKS = re.compile(r"(?i)^(esse é um resumo|esse e um resumo|demandas ativas agora|itens já resolvidos|itens ja resolvidos|outras demandas novas)$")

_GENERIC_TITLE_CANDIDATES = {
    "briefing", "keyframe", "keyframes", "reuniao", "reuniao com a 3k", "reunião", "reunião com a 3k",
    "entregue", "quase", "isso", "mas ainda nao", "mas ainda não", "audio", "áudio", "storyboard"
}
_TITLE_STOPWORDS = {
    "status", "ativa", "ativo", "agora", "demanda", "demandas", "aberto", "aberta", "pendente", "prioridade", "estimativa",
    "agendamento", "reuniao", "reunião", "feito", "feita", "terminei", "entregou", "entreguei", "ja", "já", "foi", "esta", "está",
    "com", "para", "sobre", "detalhe", "falta", "hoje", "rig", "mandei", "combinei", "andamento", "ativo", "resolvido",
    "resolvida", "concluido", "concluida", "concluído", "concluída", "enviado", "enviados", "assets", "prontos", "chegaram",
    "quase", "mas", "ainda", "nao", "não", "continua", "segue", "rolando", "voltou", "travado", "pausou", "startado", "startei"
}


def split_update_chunks(raw_text: str) -> list[str]:
    normalized = raw_text.replace("\t", " ")
    raw_parts = re.split(r"\n+|•|\*|;", normalized)
    parts: list[str] = []
    for part in raw_parts:
        cleaned = part.strip(" -–—:\n")
        if cleaned:
            parts.append(cleaned)
    return parts


def looks_like_field_line(chunk: str) -> bool:
    return bool(_FIELD_LINE_PREFIXES.match(chunk))


def looks_like_section_header(chunk: str) -> bool:
    normalized = task_manager.normalize_task_title(chunk)
    return normalized in {
        "demandas ativas agora",
        "outras demandas novas",
        "itens ja resolvidos",
        "itens resolvidos",
        "galaxy fire abertura",
        "spark",
        "galaxy",
        "cast",
    }


def field_line_note(chunk: str) -> str | None:
    match = _FIELD_LINE_PREFIXES.match(chunk)
    if not match:
        return None
    return chunk.strip()


def detect_status(chunk: str) -> str | None:
    normalized = task_manager.normalize_task_title(chunk)
    if _NEGATED_NOT_STARTED.search(chunk):
        return "pending"
    if _NEGATED_DONE.search(chunk):
        return "in_progress"
    if _NEGATED_PENDING_TO_DONE.search(chunk) and ("entreg" in normalized or "resolve" in normalized or "conclu" in normalized):
        return "done"
    if "nao esta mais ativo" in normalized or "não está mais ativo" in chunk.lower():
        return "done"
    if _STATUS_PATTERNS["done_external"].search(chunk):
        return "done"
    if _STATUS_PATTERNS["in_progress"].search(chunk):
        return "in_progress"
    if _STATUS_PATTERNS["done"].search(chunk):
        return "done"
    if _STATUS_PATTERNS["pending"].search(chunk):
        return "pending"
    return None


def extract_note(chunk: str) -> str | None:
    lowered = chunk.lower()
    notes = []
    if "3k" in lowered and ("11h" in lowered or "11 h" in lowered):
        notes.append("Reunião com a 3K marcada para segunda às 11h")
    if "storyboard" in lowered:
        notes.append(chunk)
    if "briefing" in lowered and ("keyframe" in lowered or "keyframes" in lowered):
        notes.append("Briefing e keyframes enviados")
    elif "briefing" in lowered or "keyframe" in lowered or "keyframes" in lowered:
        notes.append(chunk)
    if "rig" in lowered:
        notes.append(chunk)
    return " | ".join(dict.fromkeys(notes)) if notes else None


def extract_estimated_minutes(chunk: str) -> int | None:
    match = re.search(r"~?\s*(\d+)h(?:\s*(\d+))?", chunk, re.IGNORECASE)
    if match:
        hours = int(match.group(1))
        extra = int(match.group(2)) if match.group(2) else 0
        return hours * 60 + extra
    match_min = re.search(r"~?\s*(\d+)\s*min", chunk, re.IGNORECASE)
    if match_min:
        return int(match_min.group(1))
    return None


def extract_title_candidate(chunk: str) -> str | None:
    text = re.sub(r"(?i)^detalhe:\s*", "", chunk).strip()
    normalized_text = task_manager.normalize_task_title(text)
    if normalized_text.startswith("esse e um resumo"):
        return None

    for sep in (" — ", " - ", " – ", ":"):
        if sep in text:
            left = text.split(sep, 1)[0].strip()
            if len(left) >= 3:
                return task_manager.canonicalize_task_title(left)

    original = text
    cleaned = task_manager.normalize_task_title(text)
    phrase_noise = [
        "ainda nao terminei", "nao terminei", "nao conclui", "nao entreguei", "ainda falta", "nao comecei",
        "esta em andamento", "esta andando", "esta rolando", "estao rolando", "segue em andamento", "segue", "continua",
        "terminei", "finalizei", "conclui", "entreguei", "foi entregue", "foi aprovado", "resolvido", "concluido",
        "pendente", "ativo", "em andamento", "startado", "startei", "comecei", "iniciei", "ja foi startado",
        "ja foi", "voltou para pendente", "assets prontos", "assets chegaram", "enviado", "enviados"
    ]
    for phrase in phrase_noise:
        cleaned = cleaned.replace(phrase, " ")
    tokens = [w for w in cleaned.split() if w and w not in _TITLE_STOPWORDS]
    if not tokens:
        return None
    candidate = " ".join(tokens[:8]).strip()
    if candidate in _GENERIC_TITLE_CANDIDATES:
        return candidate

    original_tokens = re.findall(r"[A-Za-zÀ-ÿ0-9|/]+", original)
    filtered_original = [w for w in original_tokens if task_manager.normalize_task_title(w) not in _TITLE_STOPWORDS]
    if filtered_original:
        rebuilt = " ".join(filtered_original[:8]).strip()
        if rebuilt:
            return task_manager.canonicalize_task_title(rebuilt)
    return task_manager.canonicalize_task_title(candidate) if candidate else None


def match_task_for_chunk(chunk: str, title_candidate: str | None, tasks: list, include_system: bool = False):
    if not tasks:
        return None
    lowered_chunk = task_manager.normalize_task_title(chunk)
    canonical_candidate = task_manager.canonicalize_task_title(title_candidate) if title_candidate else None
    best_task = None
    best_score = 0.0

    for task in tasks:
        if not include_system and (task.category in ("backlog", "system") or task_manager.is_system_task_title(task.title or "")):
            continue
        title = task.title or ""
        normalized_title = task_manager.normalize_task_title(task_manager.canonicalize_task_title(title))
        score = 0.0
        if canonical_candidate and task_manager.titles_look_similar(title, canonical_candidate):
            score += 24
        ratio = SequenceMatcher(None, lowered_chunk[:160], normalized_title).ratio()
        score += ratio * 8
        keywords = [w for w in lowered_chunk.split() if w and w not in _TITLE_STOPWORDS]
        overlap = sum(1 for kw in keywords if kw in normalized_title)
        score += overlap * 3
        if "motion avisos" in lowered_chunk and "motion avisos" in normalized_title:
            score += 14
        if "avisos do spark" in lowered_chunk and "motion avisos" in normalized_title:
            score += 14
        if "countdown" in lowered_chunk and "countdown" in normalized_title:
            score += 8
        if "screensaver" in lowered_chunk and "screensaver" in normalized_title:
            score += 8
        if ("video de abertura" in lowered_chunk or "abertura fire" in lowered_chunk or "projeto da 3k" in lowered_chunk or "3k" in lowered_chunk) and "video de abertura" in normalized_title:
            score += 16
        if score > best_score:
            best_score = score
            best_task = task

    if best_score < 8:
        return None
    return best_task


def infer_category(chunk: str) -> str:
    if _SYSTEM_HINTS.search(chunk):
        return "system"
    return "work"


def is_note_only_candidate(title: str | None) -> bool:
    if not title:
        return False
    normalized = task_manager.normalize_task_title(title)
    generic = {task_manager.normalize_task_title(x) for x in _GENERIC_TITLE_CANDIDATES}
    return normalized in generic


def should_skip_chunk(stripped: str) -> bool:
    return bool(_SKIP_UPDATE_CHUNKS.match(stripped) or _IGNORE_CONTEXT_CHUNKS.match(stripped))
