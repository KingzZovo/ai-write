"""Real author transactions and durable outbox handoff in isolated PG schemas."""
import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update

from app.models.project import Chapter, Volume
from app.models.entity_extraction import ChapterEntityExtraction as Record, EntitySourceOutbox as Outbox, entity_projection_tokens
from app.services import entity_outbox as eo
from tests.test_pipeline_control import runtime_db


@pytest_asyncio.fixture
async def env(runtime_db):
    r = runtime_db
    spec = importlib.util.spec_from_file_location('outbox_migration',
        Path(__file__).parents[1] / 'alembic/versions/a1001925_entity_source_outbox.py')
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    async with r.engine.begin() as conn:
        def install(sync):
            entity_projection_tokens.create(sync, checkfirst=True)
            Record.__table__.create(sync, checkfirst=True)
            migration.op = Operations(MigrationContext.configure(sync)); migration.upgrade()
        await conn.run_sync(install)
    r.migration = migration
    return r


async def save(r, **values):
    async with r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(**values))
        await db.commit()


async def complete(r):
    await save(r, status='completed', global_idx=1, content_text='江枫回家。')


async def pending(r):
    async with r.sessions() as db:
        return await db.scalar(select(Outbox))


@pytest.mark.asyncio
async def test_author_rollback_and_precommit_consumer_see_no_notification(env):
    r = env
    async with r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id == r.chapter)
                         .values(status='completed', global_idx=1))
        assert await db.scalar(select(Outbox)) is not None
        assert await pending(r) is None
        assert (await eo.drain_entity_source_outbox(r.sessions))['examined'] == 0
        await db.rollback()
    assert await pending(r) is None
    await complete(r)
    assert await pending(r) is not None


@pytest.mark.asyncio
async def test_handoff_needs_no_broker_and_is_idempotent(env):
    r = env
    await complete(r)
    assert (await eo.drain_entity_source_outbox(r.sessions))['transferred'] == 1
    assert await pending(r) is None
    async with r.sessions() as db:
        record = await db.scalar(select(Record))
        assert record.status == 'pending' and record.source_json['chapter_idx'] == 1
        record.status = 'completed'; await db.commit()
        # Duplicate capture must not reset a completed payload or force model IO.
        db.add(Outbox(chapter_id=r.chapter)); await db.commit()
    await eo.drain_entity_source_outbox(r.sessions)
    async with r.sessions() as db:
        records = (await db.scalars(select(Record))).all()
        assert len(records) == 1 and records[0].status == 'completed'


@pytest.mark.asyncio
async def test_handoff_failure_rolls_back_receipt_and_ack(env, monkeypatch):
    r = env
    await complete(r)
    from sqlalchemy.ext.asyncio import AsyncSession
    original = AsyncSession.commit
    async def fail(self):
        raise RuntimeError('simulated process failure before commit')
    monkeypatch.setattr(AsyncSession, 'commit', fail)
    with pytest.raises(RuntimeError): await eo.drain_entity_source_outbox(r.sessions)
    monkeypatch.setattr(AsyncSession, 'commit', original)
    assert await pending(r) is not None
    async with r.sessions() as db:
        assert await db.scalar(select(Record)) is None
    assert (await eo.drain_entity_source_outbox(r.sessions))['transferred'] == 1


@pytest.mark.asyncio
async def test_concurrent_new_body_is_not_acknowledged_as_old_source(env, monkeypatch):
    r = env
    await complete(r)
    old = await pending(r)
    original = eo.resolve_entity_source
    async def racing(*args, **kwargs):
        source = await original(*args, **kwargs)
        await save(r, content_text='江枫离家。')
        return source
    monkeypatch.setattr(eo, 'resolve_entity_source', racing)
    await eo.drain_entity_source_outbox(r.sessions)
    assert (await pending(r)).revision > old.revision
    monkeypatch.setattr(eo, 'resolve_entity_source', original)
    await eo.drain_entity_source_outbox(r.sessions)
    assert await pending(r) is None
    async with r.sessions() as db:
        assert len((await db.scalars(select(Record))).all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('title','新标题'), ('chapter_idx',2), ('global_idx',2), ('content_text','新正文。')])
async def test_source_changes_coalesce_with_new_revision(env, field, value):
    await complete(env); old = await pending(env)
    await save(env, **{field:value})
    assert (await pending(env)).revision > old.revision


@pytest.mark.asyncio
async def test_summary_only_edit_does_not_enqueue_entity_work(env):
    await complete(env)
    await eo.drain_entity_source_outbox(env.sessions)
    await save(env, summary='更新摘要', word_count=100)
    assert await pending(env) is None


@pytest.mark.asyncio
async def test_volume_reordering_and_chapter_move_capture_source(env):
    r = env
    await complete(r); old = await pending(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        await db.execute(update(Volume).where(Volume.id == chapter.volume_id).values(volume_idx=2))
        await db.commit()
    assert (await pending(r)).revision > old.revision
    await eo.drain_entity_source_outbox(r.sessions)
    async with r.sessions() as db:
        new = Volume(project_id=r.project, volume_idx=3, title='第三卷'); db.add(new); await db.flush()
        await db.execute(update(Chapter).where(Chapter.id == r.chapter).values(volume_id=new.id))
        await db.commit()
    assert await pending(r) is not None


@pytest.mark.asyncio
async def test_unresolved_source_stays_repairable_without_payload(env):
    await complete(env); await save(env, global_idx=None)
    assert (await eo.drain_entity_source_outbox(env.sessions))['transferred'] == 0
    assert await pending(env) is not None
    await save(env, global_idx=1)
    assert (await eo.drain_entity_source_outbox(env.sessions))['transferred'] == 1


@pytest.mark.asyncio
async def test_insert_capture_and_migration_roundtrip_keep_author_content(env):
    r = env
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        new = Chapter(volume_id=chapter.volume_id, chapter_idx=2, global_idx=2,
                      title='新章', content_text='原文。', status='completed')
        db.add(new); await db.commit()
        assert (await db.scalars(select(Outbox.chapter_id))).all() == [new.id]
    async with r.engine.begin() as conn:
        def cycle(sync):
            r.migration.op = Operations(MigrationContext.configure(sync))
            r.migration.downgrade(); r.migration.upgrade()
        await conn.run_sync(cycle)
    async with r.sessions() as db:
        assert (await db.get(Chapter, new.id)).content_text == '原文。'
    await save(r, status='completed', global_idx=1)
    assert await pending(r) is not None
