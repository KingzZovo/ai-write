"""Real row locks and transaction release; collected but skipped offline."""
import asyncio
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import insert, update

from app.models.cognition import ChapterCognition as Record
from app.models.project import Chapter
from app.services import arc_planning_execution as execution
from app.services.arc_planning_contract import source_token
from app.services.arc_planning_requests import author_request_command
from app.services.arc_planning_sources import read_planning_source
from app.services.chapter_order import lock_chapter_structure
from app.services.cognition_edits import save_cognition_decision
from app.services.cognition_reader import record_version
from app.services.entity_sources import resolve_entity_source
from tests.support.planning_cognition import cognition_values
from tests.test_arc_planning_pg import planning_pg, post, run, snapshot
from tests.test_pipeline_control import runtime_db

pytestmark = [pytest.mark.asyncio, pytest.mark.external_services]


async def ready(r):
    text = '林禾读完回条，知道信还没有寄出。'
    async with r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(content_text=text, status='completed'))
        await db.commit()
        source = await resolve_entity_source(db, r.project, 0, r.chapter)
        values = cognition_values(source, [dict(character='林禾', learns='信还没有寄出', evidence=text)])
        await db.execute(insert(Record).values(**values)); await db.commit()
        row = await db.get(Record, values['id'])
        version = record_version(row, source)
        planning_source = await read_planning_source(db, r.project)
    r.body = r.body.model_copy(update={'expected_source': source_token(r.project, planning_source)})
    return NS(source=source, record_id=values['id'], version=version)


async def test_final_planning_snapshot_blocks_official_cognition_edit_until_release(planning_pg):
    r = planning_pg; data = await ready(r)
    entered = asyncio.Event()
    async def edit():
        async with r.sessions() as db:
            result = await save_cognition_decision(db, r.project, r.chapter, uuid4(), data.source.source_hash,
                data.version, mode='author', changes=[], actor='隔离编辑')
            entered.set(); await db.commit()
            return result
    async with r.sessions() as db:
        await lock_chapter_structure(db, r.project)
        original = await read_planning_source(db, r.project, lock=True)
        task = asyncio.create_task(edit())
        try:
            await asyncio.sleep(0.1)
            assert not task.done() and not entered.is_set()
            assert await read_planning_source(db, r.project, lock=True) == original
        finally:
            await db.commit()
            await asyncio.wait_for(task, 10)
    async with r.sessions() as db: current = await read_planning_source(db, r.project)
    assert current['cognition_context']['ledger'] == {}
    assert current['cognition_context']['snapshot_hash'] != original['cognition_context']['snapshot_hash']


async def test_cognition_selection_releases_row_transaction_for_author_cancel(planning_pg, monkeypatch):
    r = planning_pg; await ready(r)
    assert (await post(r))[0] == 202
    entered, release = asyncio.Event(), asyncio.Event()
    original = execution.prepare_planning_cognition
    async def hold(source, query, *, db, **kwargs):
        assert not db.in_transaction() and not db.bind.in_transaction()
        entered.set(); await release.wait()
        return await original(source, query, db=db, **kwargs)
    monkeypatch.setattr(execution, 'prepare_planning_cognition', hold)
    task = asyncio.create_task(run(r))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with r.sessions() as db:
            row, _ = await author_request_command(db, r.project, r.body.request_id, 'cancel')
            assert row.status == 'cancelled'; await db.commit()
    finally:
        release.set()
        await asyncio.wait_for(task, 10)
    assert (await snapshot(r))['status'] == 'cancelled' and not r.calls
