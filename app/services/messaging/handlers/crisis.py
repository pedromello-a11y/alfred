"""
Handlers para modo crise e fluxo de destravamento (unstuck).
"""
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import brain, task_manager

_CRISIS_RECOVERY_PATTERNS = re.compile(
    r"(?i)(melhorei|tô melhor|to melhor|me sinto melhor|voltei|pronto pra trabalhar|"
    r"pode voltar ao normal|cancela modo crise|sai do modo crise)"
)


async def handle_crisis_message(raw_text: str, db: AsyncSession) -> str:
    if _CRISIS_RECOVERY_PATTERNS.search(raw_text):
        await task_manager.set_setting("crisis_mode", "false", db)
        await task_manager.set_setting("crisis_since", "", db)
        return "Fico feliz! 🙌 Voltamos ao ritmo normal. Quando quiser ver suas tarefas, é só pedir."
    prompt = (
        "Pedro está passando por um período difícil (modo crise ativo). "
        "Responda de forma empática e gentil, sem mencionar tarefas, backlog ou produtividade. "
        "Mensagem dele: " + raw_text
    )
    return await brain.casual_response(prompt, db=db)


async def handle_unstuck_flow(raw_text: str, db: AsyncSession) -> str:
    step = int(await task_manager.get_setting("unstuck_step", "1", db=db) or "1")
    if step == 1:
        await task_manager.set_setting("unstuck_task", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "2", db)
        return "Qual o menor pedaço que dá pra fazer em 5 minutos?"
    if step == 2:
        await task_manager.set_setting("unstuck_micro", raw_text[:100], db)
        await task_manager.set_setting("unstuck_step", "3", db)
        return "Faz só isso agora. Me avisa quando terminar. 🎯"
    if step == 3:
        await task_manager.set_setting("unstuck_step", "4", db)
        return "Show! ✅ Quer fazer mais 5 minutos ou parar aqui?"
    await task_manager.set_setting("unstuck_mode", "false", db)
    await task_manager.set_setting("unstuck_step", "1", db)
    if any(w in raw_text.lower() for w in ("mais", "continuar", "seguir", "sim")):
        return "Bora! Qual o próximo micro-passo?"
    return "Ótimo trabalho. Quando quiser continuar, é só falar."
