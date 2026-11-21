"""Real PostgreSQL locks/unique receipts. Collected, skipped in offline mode."""
import asyncio
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.models.cognition import ChapterCognition as Record, CognitionRequest as Receipt, CognitionRevision as Revision
from app.services import cognition_extraction as extraction
from app.services.cognition_reader import read_chapter_cognition
from app.services.cognition_requests import retry_cognition, CognitionConflict
from app.services.cognition_edits import save_cognition_decision
from tests.test_cognition_sources import cog_db, source, runtime_db

pytestmark = [pytest.mark.asyncio, pytest.mark.external_services]


async def failed_view(r):
    src = await source(r)
    async with r.sessions() as db:
        await extraction.enqueue_cognition(db, src)
        await db.execute(update(Record).values(status='failed', attempt_count=4))
        await db.commit()
        return await read_chapter_cognition(db, r.project, r.chapter)


async def submit(r, view, request_id):
    async with r.sessions() as db:
        result = await retry_cognition(db, r.project, r.chapter, request_id, view['source_hash'], view['version'])
        await db.commit()
        return result


async def test_concurrent_same_request_resets_one_cycle_only(cog_db):
    r = cog_db
    view, request_id = await failed_view(r), uuid4()
    results = await asyncio.wait_for(asyncio.gather(submit(r, view, request_id), submit(r, view, request_id)), 5)
    assert results[0] == results[1]
    async with r.sessions() as db:
        row = await db.scalar(select(Record))
        assert str(row.cycle_id) == results[0]['cycle_id'] and row.attempt_count == 0
        assert len((await db.execute(select(Receipt.id))).scalars().all()) == 1


async def test_concurrent_different_requests_cannot_both_reset_same_version(cog_db):
    r = cog_db
    view = await failed_view(r)
    results = await asyncio.wait_for(asyncio.gather(
        submit(r, view, uuid4()), submit(r, view, uuid4()), return_exceptions=True), 5)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CognitionConflict) for result in results) == 1
    async with r.sessions() as db:
        assert len((await db.execute(select(Receipt.id))).scalars().all()) == 1
        assert (await db.scalar(select(Record))).attempt_count == 0


async def test_late_worker_cannot_overwrite_author_retry_while_holding_lease(cog_db, monkeypatch):
    r = cog_db
    src = await source(r)
    started, release = asyncio.Event(), asyncio.Event()
    async def model(**kwargs):
        started.set()
        await release.wait()
        return NS(text='[]', finish_reason='stop')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    old = asyncio.create_task(extraction.run_cognition_extraction(src, sessions=r.sessions, engine=r.engine))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with r.sessions() as db:
            await db.execute(update(Record).values(status='failed'))
            await db.commit()
            view = await read_chapter_cognition(db, r.project, r.chapter)
        accepted = await asyncio.wait_for(submit(r, view, uuid4()), 5)
        release.set()
        assert (await asyncio.wait_for(old, 5))['status'] == 'superseded'
        async with r.sessions() as db:
            row = await db.scalar(select(Record))
            assert row.status == 'pending' and str(row.cycle_id) == accepted['cycle_id']
            assert row.changes_json is None and row.attempt_count == 0
    finally:
        release.set()
        if not old.done(): old.cancel()
        await asyncio.gather(old, return_exceptions=True)


async def author_submit(r, view, request_id, changes):
    async with r.sessions() as db:
        result = await save_cognition_decision(db, r.project, r.chapter, request_id,
            view['source_hash'], view['version'], mode='author', changes=changes, actor='作者')
        await db.commit()
        return result


async def test_same_author_request_concurrently_appends_one_revision(cog_db):
    r = cog_db
    view, request_id = await failed_view(r), uuid4()
    results = await asyncio.wait_for(asyncio.gather(
        author_submit(r, view, request_id, []), author_submit(r, view, request_id, [])), 5)
    assert results[0] == results[1]
    async with r.sessions() as db:
        assert len((await db.execute(select(Revision.id))).scalars().all()) == 1
        assert (await read_chapter_cognition(db, r.project, r.chapter))['changes'] == []


async def test_two_author_decisions_from_same_version_have_one_winner(cog_db):
    r = cog_db
    view = await failed_view(r)
    changes = [dict(character='沈青', still_unknown='父亲收到信', evidence='沈青不知道父亲已经收到信。')]
    results = await asyncio.wait_for(asyncio.gather(author_submit(r, view, uuid4(), []),
        author_submit(r, view, uuid4(), changes), return_exceptions=True), 5)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CognitionConflict) for result in results) == 1
    async with r.sessions() as db:
        assert len((await db.execute(select(Revision.id))).scalars().all()) == 1
        assert len((await db.execute(select(Receipt.id))).scalars().all()) == 1


async def test_author_correction_is_not_blocked_or_overwritten_by_inflight_model(cog_db, monkeypatch):
    r = cog_db
    src = await source(r)
    started, release = asyncio.Event(), asyncio.Event()
    async def model(**kwargs):
        started.set()
        await release.wait()
        return NS(text='[]', finish_reason='stop')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', model)
    worker = asyncio.create_task(extraction.run_cognition_extraction(src, sessions=r.sessions, engine=r.engine))
    try:
        await asyncio.wait_for(started.wait(), 5)
        async with r.sessions() as db:
            view = await read_chapter_cognition(db, r.project, r.chapter)
        accepted = await asyncio.wait_for(author_submit(r, view, uuid4(), []), 5)
        release.set()
        assert (await asyncio.wait_for(worker, 5))['status'] == 'superseded'
        async with r.sessions() as db:
            detail = await read_chapter_cognition(db, r.project, r.chapter)
            assert detail['source_mode'] == 'author' and detail['revision_id'] == accepted['revision_id']
            assert detail['changes'] == [] and detail['model_changes'] is None
    finally:
        release.set()
        if not worker.done(): worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
