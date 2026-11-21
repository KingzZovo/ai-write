"""Real connection-loss races: a late attempt cannot overwrite its successor."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import select, text, update
from app.models.cognition import ChapterCognition as Record
from app.services import cognition_extraction as cog
from tests.test_cognition_sources import cog_db, source
from tests.test_pipeline_control import runtime_db

@pytest.mark.asyncio
async def test_old_failure_after_lease_loss_cannot_overwrite_new_completion(cog_db,monkeypatch):
    r=cog_db;src=await source(r)
    started=asyncio.Event();release=asyncio.Event();leases=[]
    original=cog.entity_project_lock
    @asynccontextmanager
    async def lock(*args):
        async with original(*args) as lease:
            leases.append(lease)
            yield lease
    monkeypatch.setattr(cog,'entity_project_lock',lock)
    calls=0
    async def model(**kwargs):
        nonlocal calls
        calls+=1
        if calls==1:
            started.set();await release.wait()
        return SimpleNamespace(text='[]')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    old=asyncio.create_task(cog.run_cognition_extraction(src,sessions=r.sessions,engine=r.engine))
    await asyncio.wait_for(started.wait(),5)
    try:
        pid=await leases[0].connection.scalar(text('SELECT pg_backend_pid()'))
        await leases[0].connection.commit()
        async with r.engine.begin() as conn:
            assert await conn.scalar(text('SELECT pg_terminate_backend(:pid)'),{'pid':pid})
        result=await cog.run_cognition_extraction(src,sessions=r.sessions,engine=r.engine)
        assert result['status']=='completed'
        async with r.sessions() as db:
            winner=await db.scalar(select(Record));token=winner.attempt_token
        release.set()
        with pytest.raises(Exception):await old
        async with r.sessions() as db:
            winner=await db.scalar(select(Record))
            assert winner.status=='completed' and winner.attempt_token==token
            assert winner.changes_json==[] and winner.last_error is None
    finally:
        release.set()
        if not old.done():old.cancel()
        await asyncio.gather(old,return_exceptions=True)

@pytest.mark.asyncio
async def test_two_recovery_dispatchers_only_publish_once(cog_db):
    r=cog_db;src=await source(r)
    async with r.sessions() as db:
        await cog.enqueue_cognition(db,src);await db.commit()
    sent=[]
    results=await asyncio.gather(*[cog.replay_pending_cognition(r.sessions,
        dispatch=lambda *args:sent.append(args)) for _ in range(2)])
    assert sum(x['dispatched'] for x in results)==1 and len(sent)==1

@pytest.mark.asyncio
async def test_obsolete_classification_invalidates_inflight_attempt(cog_db,monkeypatch):
    from app.tasks.entity_tasks import _extract_cognition_async
    from uuid import uuid4
    r=cog_db;src=await source(r)
    async with r.sessions() as db:
        await cog.enqueue_cognition(db,src)
        await db.execute(update(Record).values(status='running',attempt_token=uuid4()))
        from app.models.project import Chapter
        await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(content_text='作者改稿。'))
        await db.commit()
    monkeypatch.setattr('app.db.session.async_session_factory',r.sessions)
    result=await _extract_cognition_async(str(r.project),str(r.chapter),src.source_hash)
    assert result['status']=='obsolete'
    async with r.sessions() as db:
        record=await db.scalar(select(Record))
        assert record.status=='obsolete' and record.attempt_token is None

@pytest.mark.asyncio
async def test_restore_during_old_attempt_does_not_claim_false_completion(cog_db,monkeypatch):
    from app.tasks.entity_tasks import _extract_cognition_async
    from app.models.project import Chapter
    r=cog_db;src=await source(r)
    monkeypatch.setattr('app.db.session.async_session_factory',r.sessions)
    async def model(**kwargs):
        async with r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(content_text='暂时改稿。'));await db.commit()
        await _extract_cognition_async(str(r.project),str(r.chapter),src.source_hash)
        async with r.sessions() as db:
            await db.execute(update(Chapter).where(Chapter.id==r.chapter).values(content_text=src.content));await db.commit()
            await cog.enqueue_cognition(db,src);await db.commit()
        return SimpleNamespace(text='[]')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    result=await cog.run_cognition_extraction(src,sessions=r.sessions,engine=r.engine)
    assert result['status']=='superseded'
    async with r.sessions() as db:
        row=await db.scalar(select(Record))
        assert row.status=='pending' and row.changes_json is None

@pytest.mark.asyncio
async def test_attempt_migration_roundtrip_preserves_source_payload(cog_db):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    r=cog_db;src=await source(r)
    async with r.sessions() as db:
        await cog.enqueue_cognition(db,src);await db.commit()
    spec=importlib.util.spec_from_file_location('attempt_migration',Path(__file__).parents[1]/'alembic/versions/a1001930_cognition_attempt_token.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    async with r.engine.begin() as conn:
        def cycle(sync):
            migration.op=Operations(MigrationContext.configure(sync))
            migration.downgrade();migration.upgrade()
        await conn.run_sync(cycle)
    async with r.sessions() as db:
        row=await db.scalar(select(Record))
        assert row.source_hash==src.source_hash and row.source_json==src.receipt
        assert row.status=='pending' and row.attempt_token is None

@pytest.mark.asyncio
async def test_chapter_deleted_before_lease_acquisition_is_obsolete(cog_db,monkeypatch):
    from sqlalchemy import delete
    from app.models.project import Chapter
    r=cog_db;src=await source(r);original=cog.entity_project_lock
    @asynccontextmanager
    async def lock(*args):
        async with r.sessions() as db:
            await db.execute(delete(Chapter).where(Chapter.id==r.chapter));await db.commit()
        async with original(*args) as lease:yield lease
    monkeypatch.setattr(cog,'entity_project_lock',lock)
    result=await cog.run_cognition_extraction(src,sessions=r.sessions,engine=r.engine)
    assert result['status']=='obsolete'

@pytest.mark.asyncio
async def test_session_cleanup_error_after_commit_does_not_downgrade_completion(cog_db,monkeypatch):
    r=cog_db;src=await source(r)
    @asynccontextmanager
    async def sessions():
        async with r.sessions() as db:
            yield db
            completed = await db.scalar(select(Record.status)) == 'completed'
        if completed:raise RuntimeError('post-commit cleanup failure')
    async def model(**kwargs):return SimpleNamespace(text='[]')
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt',model)
    with pytest.raises(RuntimeError,match='post-commit cleanup'):
        await cog.run_cognition_extraction(src,sessions=sessions,engine=r.engine)
    async with r.sessions() as db:
        row=await db.scalar(select(Record))
        assert row.status=='completed' and row.changes_json==[] and row.last_error is None
