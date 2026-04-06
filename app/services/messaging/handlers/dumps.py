"""
Handler para brain dumps (explícitos via "dump:" e referências/ideias).
"""
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import agenda_manager, dump_manager, task_manager
from app.services.messaging.handlers.utils import agenda_blocks_inline

_DUMP_PREFIX = re.compile(r"(?i)^dump:\s*")


async def handle_dump(raw_stripped: str, origin: str, db: AsyncSession | None) -> str:
    dump_text = _DUMP_PREFIX.sub("", raw_stripped).strip()
    if not dump_text:
        return "Dump vazio — manda o que quer registrar depois de 'dump:'"
    if db is None:
        return "Registrado em dumps."

    item = await dump_manager.create_dump_item(raw_stripped, origin, db)
    current_block = await agenda_manager.get_current_agenda_block(db)
    if current_block and current_block.block_type == "break":
        focus_line = f"Segue no seu descanso: *{current_block.title}*."
    elif current_block:
        focus_line = f"Depois volta pra *{current_block.title}*."
    else:
        pending = await task_manager.get_active_tasks(db)
        focus_line = f"Volta pra *{pending[0].title}*." if pending else "Isso não vai se perder."

    return f"Registrado em dumps como *{item.rewritten_title}* ({item.category or 'desconhecido'}).\n{focus_line}"
