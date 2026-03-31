from app.services import task_manager


async def test_find_task_by_title_like_prefers_project_match(db_session):
    await task_manager.upsert_task_from_context("Spark | Motion Avisos", db_session, status="pending", category="work")
    await task_manager.upsert_task_from_context("Galaxy | Motion Avisos", db_session, status="pending", category="work")

    match = await task_manager.find_task_by_title_like("spark motion avisos", db_session, include_closed=True)

    assert match is not None
    assert match.title == "Spark | Motion Avisos"


async def test_find_task_by_title_like_avoids_generic_single_word_false_positive(db_session):
    await task_manager.upsert_task_from_context("Spark | Motion Avisos", db_session, status="pending", category="work")
    await task_manager.upsert_task_from_context("Galaxy | Motion Abertura", db_session, status="pending", category="work")

    match = await task_manager.find_task_by_title_like("motion", db_session, include_closed=True)

    assert match is None


async def test_mark_done_uses_smart_matching_with_project_and_fragment(db_session):
    await task_manager.upsert_task_from_context("Spark | Countdown", db_session, status="pending", category="work")
    await task_manager.upsert_task_from_context("Galaxy | Countdown", db_session, status="pending", category="work")

    task, xp_msg = await task_manager.mark_done("spark countdown", db_session)

    assert task is not None
    assert task.title == "Spark | Countdown"
    assert task.status == "done"
    assert "+" in xp_msg
