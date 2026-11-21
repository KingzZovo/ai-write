"""Real PostgreSQL concurrency/fencing checks, never executed with --offline."""
import asyncio
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.api.arc_planning import ArcPlanBody, submit_arc_plan
from app.models.arc_planning import ArcPlanningRequest as Request
from app.models.arc_progress import ArcProgressWrite
from app.models.cognition import ChapterCognition, CognitionRevision, CognitionRequest
from app.models.project import Outline, Volume
from app.services import arc_planning_execution as execution
from app.services.arc_planning_requests import author_request_command
from app.services.arc_planning_sources import read_planning_source
from app.services.arc_planning_contract import source_token
from app.services.chapter_order import ChapterOrderConflict, lock_chapter_structure
from app.services.generation_execution import GenerationExecutionSession, GenerationExecutionUnavailable
from tests.support.arc_outline import arc_meta, arc_chapter
from tests.test_arc_planning_journal import route
from tests.test_pipeline_control import runtime_db
from tests.support.world_rules import install_rule_graph
from tests.support.character_graph import CharacterGraph

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def planning_pg(runtime_db, monkeypatch):
    from app.api import arc_planning as api
    from unittest.mock import AsyncMock
    r = runtime_db
    r.calls, r.callback = [], None
    r.graph = install_rule_graph(monkeypatch, r.project)
    r.character_graph = CharacterGraph(r.project)
    async def character_driver(): yield r.character_graph
    monkeypatch.setattr('app.db.neo4j.get_neo4j', character_driver)
    async with r.engine.begin() as db:
        await db.run_sync(lambda connection: Request.__table__.create(connection))
        await db.run_sync(lambda connection: ArcProgressWrite.__table__.create(connection))
        for model in (ChapterCognition, CognitionRevision, CognitionRequest):
            await db.run_sync(lambda connection, model=model: model.__table__.create(connection, checkfirst=True))
    async def capture(task, db): return route(task)
    async def stage(task, user, db, **kwargs):
        assert not db.bind.in_transaction()
        r.calls.append(task)
        if r.callback is not None: await r.callback(db)
        return arc_meta(4) if task == 'arc_outline_meta' else {'chapter_summaries': [arc_chapter(i) for i in range(1, 5)]}
    monkeypatch.setattr(execution, 'capture_prompt_snapshot', capture)
    monkeypatch.setattr(execution, 'run_structured_prompt', stage)
    monkeypatch.setattr(api, 'publish_arc_request', AsyncMock())
    async with r.sessions() as db:
        source = await read_planning_source(db, r.project)
    r.body = ArcPlanBody(request_id=str(uuid4()), expected_source=source_token(r.project, source), mode='start',
                        idea='保留完整创作材料', background='前文原稿不改', target_chapters=4)
    return r


async def post(r, body=None):
    async with r.sessions() as db:
        try:
            return 202, await submit_arc_plan(r.project, body or r.body, db)
        except HTTPException as exc:
            return exc.status_code, exc.detail


async def snapshot(r):
    async with r.sessions() as db:
        row = await db.get(Request, UUID(r.body.request_id))
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


async def run(r, **kwargs):
    return await execution.run_arc_request(r.body.request_id, sessions=r.sessions, **kwargs)


@pytest.mark.asyncio
async def test_same_pg_request_race_is_one_submission_and_replay_does_not_dispatch(planning_pg):
    r = planning_pg
    results = await asyncio.gather(post(r), post(r))
    assert any(code == 202 for code, _ in results) and all(code in {202, 409} for code, _ in results)
    again = await post(r)
    assert again[0] == 202 and again[1]['dispatch_confirmed'] is None
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Request)) == 1


@pytest.mark.asyncio
async def test_different_pg_requests_cannot_both_claim_the_same_next_arc(planning_pg):
    r = planning_pg
    other = r.body.model_copy(update={'request_id': str(uuid4())})
    results = await asyncio.gather(post(r), post(r, other))
    assert sorted(code for code, _ in results) == [202, 409]
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Request)) == 1


@pytest.mark.asyncio
async def test_pg_physical_lock_excludes_workers_but_allows_author_cancellation(planning_pg):
    r = planning_pg; assert (await post(r))[0] == 202
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(db): entered.set(); await release.wait()
    r.callback = hold
    pending = asyncio.create_task(run(r))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert (await run(r, manual=True))['status'] == 'busy'
        async with r.sessions() as db:
            with pytest.raises(ChapterOrderConflict): await lock_chapter_structure(db, r.project)
            await db.rollback()
        async with r.sessions() as db:
            row, _ = await author_request_command(db, r.project, r.body.request_id, 'cancel')
            assert row.status == 'cancelled'
            await db.commit()
    finally:
        release.set()
        await asyncio.wait_for(pending, 10)
    assert (await snapshot(r))['status'] == 'cancelled' and len(r.calls) == 1
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Volume).where(Volume.project_id == r.project)) == 1


@pytest.mark.asyncio
async def test_invalidated_pg_connection_cannot_save_a_late_phase_result(planning_pg):
    r = planning_pg; assert (await post(r))[0] == 202
    async def disconnect(db): await db.bind.invalidate()
    r.callback = disconnect
    assert (await run(r))['status'] == 'unconfirmed'
    before = await snapshot(r)
    assert before['journal_json']['stages'][0]['attempts'][0]['state'] == 'reserved'
    r.callback = None
    await run(r, manual=True)
    after = await snapshot(r)
    assert after['status'] == 'completed' and after['execution_attempts'] == 2
    assert len(r.calls) == 3


@pytest.mark.asyncio
async def test_lost_pg_final_commit_reply_keeps_the_single_creation_receipt(planning_pg, monkeypatch):
    r = planning_pg; assert (await post(r))[0] == 202
    original = GenerationExecutionSession.commit
    lost = False
    async def lose_ack(db):
        nonlocal lost
        final = any(isinstance(row, Request) and row.status == 'completed' for row in db.dirty)
        await original(db)
        if final and not lost:
            lost = True; db.guard.revoke()
            raise GenerationExecutionUnavailable()
    monkeypatch.setattr(GenerationExecutionSession, 'commit', lose_ack)
    assert (await run(r))['status'] == 'unconfirmed'
    before = await snapshot(r)
    assert before['status'] == 'completed'
    await run(r, manual=True)
    assert await snapshot(r) == before and len(r.calls) == 2
    assert (await post(r))[1]['result'] == before['result_json']


@pytest.mark.asyncio
async def test_pg_source_is_rechecked_after_external_structure_changes(planning_pg):
    r = planning_pg; assert (await post(r))[0] == 202
    async def external_change(writer):
        async with r.sessions() as db:
            await db.execute(update(Volume).where(Volume.project_id == r.project).values(volume_idx=9))
            await db.commit()
    r.callback = external_change
    await run(r)
    state = await snapshot(r)
    assert state['status'] == 'needs_review' and state['error_code'] == 'arc_planning_source_changed'
    assert len(r.calls) == 1 and state['source_json']['volume_order'][0]['volume_idx'] == 1
    async with r.sessions() as db:
        assert list((await db.execute(select(Volume.volume_idx).where(Volume.project_id == r.project))).scalars()) == [9]


@pytest.mark.asyncio
async def test_pg_downgrade_retains_a_request_committed_during_the_empty_check(planning_pg, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import event
    from app.services.arc_planning_requests import prepare_arc_request

    r = planning_pg
    path = Path(__file__).parents[1] / 'alembic/versions/a1001958_arc_planning_requests.py'
    spec = importlib.util.spec_from_file_location('arc_planning_downgrade_race', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    checking = asyncio.Event()
    marker = 'arc_planning_downgrade_race'

    def before_query(connection, _cursor, statement, _params, _context, _many):
        if connection.info.get(marker) and statement.startswith('LOCK TABLE arc_planning_requests'):
            checking.set()

    def after_query(connection, _cursor, statement, _params, _context, _many):
        # The former unlocked empty read also reaches this checkpoint, then
        # DROP waits for acceptance and incorrectly discards the new request.
        if connection.info.get(marker) and statement.startswith('SELECT') and 'arc_planning_requests' in statement:
            checking.set()

    async def downgrade():
        async with r.engine.begin() as connection:
            connection.sync_connection.info[marker] = True
            try:
                def apply(sync):
                    monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(sync)))
                    migration.downgrade()
                await connection.run_sync(apply)
            finally:
                connection.sync_connection.info.pop(marker, None)

    event.listen(r.engine.sync_engine, 'before_cursor_execute', before_query)
    event.listen(r.engine.sync_engine, 'after_cursor_execute', after_query)
    pending = None
    try:
        async with r.sessions() as author:
            row, created = await prepare_arc_request(author, r.project, r.body.model_dump(mode='json'))
            assert created and row.status == 'pending'
            pending = asyncio.create_task(downgrade())
            await asyncio.wait_for(checking.wait(), 5)
            await author.commit()
        with pytest.raises(RuntimeError, match='原记录'):
            await asyncio.wait_for(pending, 5)
        state = await snapshot(r)
        assert state['request_json']['idea'] == r.body.idea and state['status'] == 'pending'
        assert state['execution_attempts'] == 0 and not r.calls
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        event.remove(r.engine.sync_engine, 'before_cursor_execute', before_query)
        event.remove(r.engine.sync_engine, 'after_cursor_execute', after_query)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['update', 'delete', 'create'])
async def test_pg_book_rule_snapshot_holds_updates_deletes_and_new_members_until_commit(planning_pg, change):
    from sqlalchemy import text
    from app.services.world_rule_writes import write_world_rule
    from app.services.world_rule_material import rule_from_properties, world_rule_version
    r = planning_pg
    # Actual PostgreSQL Project locking, with an explicit in-memory graph.
    # Real managed Cypher transactions are covered in test_world_rule_sources_pg.
    rule = {'id': str(uuid4()), 'category': '世界限制', 'text': '原规则：不可重复施术', 'origin': 'author'}
    r.graph.rules.append(rule)
    version = world_rule_version(rule_from_properties(rule))
    entered = asyncio.Future()

    async def edit():
        async with r.sessions() as db:
            entered.set_result(await db.scalar(text('SELECT pg_backend_pid()')))
            await write_world_rule(db, r.graph, r.project, change,
                category='新增限制' if change == 'create' else rule['category'],
                text='新插入的规则不能穿过最终来源检查' if change == 'create' else rule['text'],
                new_category=rule['category'], new_text='作者刚修改的规则', expected_version=version)
            await db.commit()

    task = None
    try:
        async with r.sessions() as writer:
            await lock_chapter_structure(writer, r.project)
            before = await read_planning_source(writer, r.project, lock=True)
            task = asyncio.create_task(edit())
            await wait_for_book_source_lock(r, task, entered)
            assert await read_planning_source(writer, r.project, lock=True) == before
            await writer.commit()
        await asyncio.wait_for(task, 5)
        async with r.sessions() as db:
            after = await read_planning_source(db, r.project)
        assert after != before and not r.calls
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def wait_for_book_source_lock(r, task, entered):
    from sqlalchemy import text
    backend_pid = await asyncio.wait_for(entered, 5)
    async with asyncio.timeout(5):
        while True:
            assert not task.done(), 'book source changed before the source transaction committed'
            async with r.sessions() as observer:
                waiting = await observer.scalar(text(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {'pid': backend_pid})
            if waiting == 'Lock':
                return
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_pg_confirming_another_book_candidate_waits_for_source_transaction(planning_pg):
    from sqlalchemy import text
    from app.api.outlines import confirm_outline
    r = planning_pg
    selected_id, candidate_id = uuid4(), uuid4()
    async with r.sessions() as db:
        db.add_all([
            Outline(id=selected_id, project_id=r.project, level='book', version=1,
                is_confirmed=1, content_json={'raw_text': '当前采用的完整书纲'}),
            Outline(id=candidate_id, project_id=r.project, level='book', version=2,
                is_confirmed=0, content_json={'raw_text': '作者稍后确认的新书纲'}),
        ])
        await db.commit()
    entered = asyncio.Future()

    async def confirm():
        async with r.sessions() as db:
            entered.set_result(await db.scalar(text('SELECT pg_backend_pid()')))
            await confirm_outline(str(r.project), str(candidate_id), None, db)
            await db.commit()

    task = None
    try:
        async with r.sessions() as writer:
            await lock_chapter_structure(writer, r.project)
            before = await read_planning_source(writer, r.project, lock=True)
            assert before['book_context']['outline']['id'] == str(selected_id)
            task = asyncio.create_task(confirm())
            await wait_for_book_source_lock(r, task, entered)
            assert await read_planning_source(writer, r.project, lock=True) == before
            await writer.commit()
        await asyncio.wait_for(task, 5)
        async with r.sessions() as db:
            after = await read_planning_source(db, r.project)
        assert after['book_context']['outline']['id'] == str(candidate_id)
        assert after != before and not r.calls
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
