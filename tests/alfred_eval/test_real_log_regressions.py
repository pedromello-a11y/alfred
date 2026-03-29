from sqlalchemy import select

from app.models import Task
from app.services import task_manager


RAW_BLOCK_FROM_LOG = """Demandas ativas agora
Spark
Spark | Countdown
status: em andamento
estimativa que você me passou: ~1h30 para terminar
prioridade: alta
Spark | Motion Avisos
status: aberto / próximo da fila
assets: já chegaram
estimativa: ~3h
prioridade: alta
Spark | Screensaver
status: secundário
ideia atual: melhorar o que o Rig passou
só entra se sobrar tempo
Galaxy / FIRE / abertura
Vídeo de Abertura / FIRE
status: frente estratégica ativa
briefing: pronto
cronograma: pronto
falta: 3 keyframes
preocupação principal: startar o projeto o quanto antes
3 keyframes do vídeo de abertura
status: pendente
função: orientar o storyboard da 3K
Outras demandas novas
Consultar viabilidade de fazer um turntable do Cosmos 2
status: pendente
checar:
se os arquivos estão com o Cavazza"""


async def _all_tasks(session):
    result = await session.execute(select(Task))
    return list(result.scalars().all())


async def _titles(session):
    return [task.title for task in await _all_tasks(session)]


def _normalized(values):
    return [task_manager.normalize_task_title(v) for v in values]


async def test_no_duplicate_barbara_task_in_open_activities(db_session, send):
    await send("entrou tarefa nova, preciso conversar com barbara pra alinhar identidade de motion do FIRE 26")
    _, response, _ = await send("atividades abertas")

    tasks = await _all_tasks(db_session)
    barbara = [t for t in tasks if "barbara" in task_manager.normalize_task_title(t.title)]

    assert len(barbara) == 1
    assert response.lower().count("barbara") <= 1


async def test_open_activities_does_not_hallucinate_generic_projects(db_session, send):
    await send("entrou tarefa nova, preciso conversar com barbara pra alinhar identidade de motion do FIRE 26")
    await task_manager.upsert_task_from_context(
        "áudio não funciona, adicionar na lista de ajustes do sistema",
        db_session,
        status="pending",
        category="system",
    )

    _, response, _ = await send("todas atividades que tem em aberto")
    lowered = response.lower()

    assert "galaxy" not in lowered
    assert "spark" not in lowered
    assert "hotmart" not in lowered
    assert "outros projetos no jira" not in lowered


async def test_system_adjustment_stays_out_of_active_work_view(db_session, send):
    await task_manager.upsert_task_from_context(
        "áudio não funciona, adicionar na lista de ajustes do sistema",
        db_session,
        status="pending",
        category="system",
    )
    await send("entrou tarefa nova, preciso conversar com barbara pra alinhar identidade de motion do FIRE 26")

    _, response, _ = await send("minhas tarefas ativas")
    lowered = response.lower()

    assert "audio nao funciona" not in lowered
    assert "áudio não funciona" not in lowered
    assert "barbara" in lowered


async def test_reset_command_clears_existing_tasks(db_session, send):
    await send("motion avisos está ativo")
    await send("turntable do cosmos 2 pendente")

    await send("zere os dados de sistema")

    tasks = await _all_tasks(db_session)
    assert len(tasks) == 0


async def test_long_log_block_does_not_create_status_headers_or_intro_tasks(db_session, send):
    await send(RAW_BLOCK_FROM_LOG)

    titles = await _titles(db_session)
    normalized = _normalized(titles)

    assert "status" not in normalized
    assert "spark" not in normalized
    assert not any(title.startswith("esse e um resumo") for title in normalized)
    assert not any("itens resolvidos" in title for title in normalized)
    assert not any(title == "checar" for title in normalized)


async def test_feedback_about_system_does_not_become_operational_task(db_session, send):
    await send("esses meus comentarios era pra implementar o sistema e nao adicionar como demand")
    await send("seria importante voce ser e me fazer perguntas")

    normalized = _normalized(await _titles(db_session))

    assert not any("esses meus comentarios" in title for title in normalized)
    assert not any("seria importante voce ser" in title for title in normalized)


async def test_three_short_spark_deadline_lines_materialize_three_tasks(db_session, send):
    await send("tenho Spark I Coutndown pra entregar até hoje final do dia")
    await send("Spark I Motion Aviso que tenho que entregar até hoje final do dia")
    await send("Spark I Screensaver hoje até final do dia")

    normalized = _normalized(await _titles(db_session))

    assert any("countdown" in title for title in normalized)
    assert any("motion avisos" in title or "motion aviso" in title for title in normalized)
    assert any("screensaver" in title for title in normalized)


async def test_video_opening_message_becomes_single_task_with_notes(db_session, send):
    await send("tenho Galaxy I Video de Abertura: marcar reuniao amanha as 11h com a 3k pra discurtismos o vídeo, eles ja comecaram a trabalhar e entregam o storyboard na terca feira")

    result = await db_session.execute(select(Task).where(Task.title.ilike("%Vídeo de Abertura%") | Task.title.ilike("%Video de Abertura%")))
    task = result.scalar_one_or_none()

    assert task is not None
    assert task.notes is not None
    assert "3k" in task.notes.lower()
    assert "storyboard" in task.notes.lower()

    normalized = _normalized(await _titles(db_session))
    assert not any(title.startswith("marcar reuniao") for title in normalized)


async def test_turntable_task_drops_prefix_noise(db_session, send):
    await send("outra demanda Consultar viabilidade de fazer um turntable do Cosmos 2")

    normalized = _normalized(await _titles(db_session))

    assert any("turntable do cosmos 2" in title for title in normalized)
    assert not any(title.startswith("outra demanda") for title in normalized)


async def test_fire_opening_proposals_uses_user_corrected_canonical_title(db_session, send):
    await send("preciso levantar referencias e propostas pra reuniao do FIRE na segunda, FIRE I Ato abertura")
    await send("separe assim FIRE 26 I Ato abertura - propostas")

    titles = await _titles(db_session)
    normalized = _normalized(titles)

    assert any("fire 26 i ato abertura propostas" == title or "fire 26 ato abertura propostas" == title for title in normalized)
    assert not any("preciso levantar referencias" in title for title in normalized)


async def test_media_reference_dump_does_not_show_in_active_tasks(db_session, send):
    await send("lembrar filme Pulp fiction")
    await send("nao é tarefa pessoal é só um dump pra eu lembrar dps, quero que fique categorizado em coisas que quero acessar depois, voce poderia categorizar como filme, se tiver duvidas me perguntar")

    _, response, _ = await send("minhas tarefas ativas")

    assert "pulp fiction" not in response.lower()


async def test_priorities_view_should_not_offer_empty_slot(db_session, send):
    await send("entrou tarefa nova, preciso conversar com barbara pra alinhar identidade de motion do FIRE 26")
    _, response, _ = await send("atividades abertas")

    assert "(vazio)" not in response.lower()
