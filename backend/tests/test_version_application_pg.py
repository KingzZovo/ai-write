"""Real PostgreSQL restoration and version-tree concurrency; skipped offline."""
import asyncio
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.api import versions
from app.db.session import Base, get_db
from app.models.project import Chapter, ChapterVersion
from app.services.version_control import VersionConflict, VersionControlService
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


async def version_tables(r):
    async with r.engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[ChapterVersion.__table__]))


@pytest.mark.asyncio
async def test_two_version_restorations_have_one_body_commit_and_backup(runtime_db, monkeypatch):
    r = runtime_db
    await version_tables(r)
    async with r.sessions() as db:
        before = (await db.get(Chapter, r.chapter)).content_text
        choices = [ChapterVersion(id=uuid4(), chapter_id=r.chapter, content_text=body, is_active=0)
                   for body in ('第一份恢复稿。', '第二份恢复稿。')]
        db.add_all(choices)
        await db.commit()
        ids = [row.id for row in choices]
    monkeypatch.setattr('app.services.entity_dispatch.dispatch_entity_extraction', Mock(return_value=True))
    async def database():
        async with r.sessions() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
    app = FastAPI()
    app.include_router(versions.router)
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        responses = await asyncio.gather(*[client.post(f'/api/chapters/{r.chapter}/versions/{identity}/rollback',
                                                       json={'expected_content_text': before}) for identity in ids])
    assert sorted(response.status_code for response in responses) == [200, 409]
    saved = next(response.json() for response in responses if response.status_code == 200)
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == saved['content_text']
        rows = (await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == r.chapter))).all()
        assert [str(row.id) for row in rows if row.is_active] == [saved['active_version_id']]
        backups = [row for row in rows if row.source == 'version_restore_backup']
        assert len(backups) == 1 and backups[0].content_text == before


@pytest.mark.asyncio
async def test_same_branch_creation_serializes_before_the_duplicate_check(runtime_db):
    r = runtime_db
    await version_tables(r)
    async with r.sessions() as db:
        source = ChapterVersion(id=uuid4(), chapter_id=r.chapter, content_text='分支来源。', is_active=1)
        db.add(source)
        await db.commit()
        source_id = source.id
    async def create():
        async with r.sessions() as db:
            try:
                await VersionControlService(db).create_branch(str(r.chapter), str(source_id), '另一条走向')
                await db.commit()
                return 'created'
            except VersionConflict:
                await db.rollback()
                return 'conflict'
    assert sorted(await asyncio.gather(create(), create())) == ['conflict', 'created']
    async with r.sessions() as db:
        rows = (await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == r.chapter))).all()
        assert len(rows) == 2 and sum(row.is_active for row in rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['branch', 'activate'])
async def test_waiting_version_mutation_does_not_lock_child_before_parent(runtime_db, operation):
    r = runtime_db
    await version_tables(r)
    async with r.sessions() as db:
        version = ChapterVersion(id=uuid4(), chapter_id=r.chapter, content_text='保存稿。', is_active=1)
        db.add(version)
        await db.commit()
        vid = version.id
    async with r.sessions() as holder, r.sessions() as worker, r.sessions() as observer:
        await holder.execute(select(Chapter.id).where(Chapter.id == r.chapter).with_for_update())
        worker_pid = await worker.scalar(text('SELECT pg_backend_pid()'))
        svc = VersionControlService(worker)
        work = (svc.create_branch(str(r.chapter), str(vid), '分支') if operation == 'branch'
                else svc.switch_active(str(r.chapter), str(vid)))
        pending = asyncio.create_task(work)
        try:
            async with asyncio.timeout(5):
                while not pending.done():
                    kind = await observer.scalar(text('SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid'), {'pid': worker_pid})
                    await observer.rollback()
                    if kind == 'Lock': break
                    await asyncio.sleep(0.01)
            assert not pending.done()
            await observer.execute(select(ChapterVersion.id).where(ChapterVersion.id == vid).with_for_update(nowait=True))
            await observer.rollback()
            await holder.commit()
            await pending
            await worker.commit()
        finally:
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
