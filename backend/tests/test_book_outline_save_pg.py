"""Book save PG integration gate, kept separate from offline unit coverage."""
import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import select, event

from app.models.project import Outline, Project
from app.services.book_outline_save import snapshot_book_outline, save_book_outline
from tests.test_pipeline_control import runtime_db
from tests.test_volume_creation import creation
from tests.test_book_outline_save_unit import TEXT, PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'project', 'book', 'confirmation', 'new_candidate'])
async def test_book_save_retains_old_candidates_and_rejects_source_changes(creation, change):
    r = creation
    async with r.sessions() as db:
        source = await snapshot_book_outline(db, r.pid)
    async with r.sessions() as db:
        if change == 'project': (await db.get(Project, r.pid)).target_word_count = 8000
        if change == 'book': (await db.get(Outline, r.bid)).content_json = {'raw_text': '作者改稿'}
        if change == 'confirmation': (await db.get(Outline, r.bid)).is_confirmed = 0
        if change == 'new_candidate': db.add(Outline(project_id=r.pid, level='book', content_json={'raw_text': '其他候选'}))
        await db.commit()
        old = deepcopy((await db.get(Outline, r.bid)).content_json)
    async with r.sessions() as db:
        if change == 'none':
            result = await save_book_outline(db, source, TEXT, PAYLOAD)
            await db.commit()
            assert result.version == 2 and result.is_confirmed == 0
        else:
            with pytest.raises(ValueError): await save_book_outline(db, source, TEXT, PAYLOAD)
            await db.rollback()
    async with r.sessions() as db:
        books = (await db.scalars(select(Outline).where(Outline.project_id == r.pid))).all()
        assert len(books) == (2 if change in ('none', 'new_candidate') else 1)
        assert (await db.get(Outline, r.bid)).content_json == old


@pytest.mark.asyncio
async def test_concurrent_book_saves_have_one_winner(creation):
    r = creation
    async with r.sessions() as db: source = await snapshot_book_outline(db, r.pid)
    async def save():
        async with r.sessions() as db:
            try:
                await save_book_outline(db, source, TEXT, PAYLOAD)
                await db.commit()
                return 'saved'
            except ValueError:
                await db.rollback()
                return 'stale'
    assert sorted(await asyncio.gather(save(), save())) == ['saved', 'stale']


@pytest.mark.asyncio
async def test_book_commit_failure_and_cancel_leave_no_new_candidate(creation):
    r = creation
    async with r.sessions() as db: source = await snapshot_book_outline(db, r.pid)
    async with r.sessions() as db:
        await save_book_outline(db, source, TEXT, PAYLOAD)
        def fail(session): raise RuntimeError('commit failed')
        event.listen(db.sync_session, 'before_commit', fail)
        try:
            with pytest.raises(RuntimeError): await db.commit()
        finally:
            event.remove(db.sync_session, 'before_commit', fail)
            await db.rollback()
    entered = asyncio.Event()
    async def paused_save():
        async with r.sessions() as db:
            await save_book_outline(db, source, TEXT, PAYLOAD)
            entered.set()
            await asyncio.Event().wait()
            await db.commit()
    task = asyncio.create_task(paused_save())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    async with r.sessions() as db:
        books = (await db.scalars(select(Outline).where(Outline.project_id == r.pid))).all()
        assert len(books) == 1 and books[0].id == r.bid
