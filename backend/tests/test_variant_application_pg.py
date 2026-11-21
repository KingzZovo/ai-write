"""Real PostgreSQL exclusion and conditional application; skipped offline."""
import asyncio
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.api import variants
from app.db.session import Base, get_db
from app.models.generation_run import GenerationRun
from app.models.project import Chapter, ChapterVariant, ChapterVersion
from app.services.bvsr import VariantCandidate, persist_variants
from app.services.review_contract import make_critic_report
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


async def variant_tables(r):
    async with r.engine.begin() as connection:
        await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            GenerationRun.__table__, ChapterVariant.__table__, ChapterVersion.__table__,
        ]))


@pytest.mark.asyncio
async def test_concurrent_variant_applications_have_one_commit_and_one_backup(runtime_db, monkeypatch):
    r = runtime_db
    await variant_tables(r)
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        before = chapter.content_text
        choices = [ChapterVariant(id=uuid4(), chapter_id=r.chapter, variant_idx=index,
                                  content_text=text, word_count=len(text))
                   for index, text in enumerate(('第一份候选正文。', '第二份候选正文。'))]
        db.add_all(choices)
        await db.commit()
        ids = [row.id for row in choices]
        previous_backups = set((await db.scalars(select(ChapterVersion.id).where(ChapterVersion.chapter_id == r.chapter))).all())
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
    app.include_router(variants.router)
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        results = await asyncio.gather(*[
            client.post(f'/api/variants/{identity}/select', json={'expected_content_text': before}) for identity in ids
        ])
    assert sorted(response.status_code for response in results) == [200, 409]
    winner_id = next(response.json()['id'] for response in results if response.status_code == 200)
    async with r.sessions() as db:
        rows = (await db.scalars(select(ChapterVariant).where(ChapterVariant.id.in_(ids)))).all()
        winner = next(row for row in rows if str(row.id) == winner_id)
        assert [str(row.id) for row in rows if row.selected_by_user] == [winner_id]
        assert (await db.get(Chapter, r.chapter)).content_text == winner.content_text
        backups = (await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == r.chapter))).all()
        new_backups = [row for row in backups if row.id not in previous_backups]
        assert len(new_backups) == 1 and new_backups[0].content_text == before


@pytest.mark.asyncio
async def test_refresh_waits_for_chapter_without_locking_existing_candidates(runtime_db):
    """A waiting refresh must not hold the child needed by the current writer."""
    r = runtime_db
    await variant_tables(r)
    async with r.sessions() as db:
        run = GenerationRun(id=uuid4(), project_id=r.project, chapter_id=r.chapter)
        db.add(run)
        await db.flush()
        existing = ChapterVariant(id=uuid4(), chapter_id=r.chapter, run_id=run.id,
                                  variant_idx=0, content_text='已交付的第一稿。')
        db.add(existing)
        await db.commit()
        run_id, existing_id = run.id, existing.id
    candidates = [VariantCandidate(index, body,
        make_critic_report(body, issues=[], stages=[{'name': 'critic', 'status': 'complete'}]))
        for index, body in enumerate(('已交付的第一稿。', '本次新增的第二稿。'))]
    async with r.sessions() as owner, r.sessions() as worker, r.sessions() as observer:
        await owner.execute(select(Chapter.id).where(Chapter.id == r.chapter).with_for_update())
        worker_pid = await worker.scalar(text('SELECT pg_backend_pid()'))
        pending = asyncio.create_task(persist_variants(worker, chapter_id=r.chapter, run_id=run_id,
            candidates=candidates, winner_idx=0, refresh_existing=True))
        try:
            async with asyncio.timeout(5):
                while not pending.done():
                    wait_type = await observer.scalar(text('SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid'),
                                                      {'pid': worker_pid})
                    await observer.rollback()
                    if wait_type == 'Lock':
                        break
                    await asyncio.sleep(0.01)
            assert not pending.done(), 'Refresh must wait for the chapter writer.'
            # This NOWAIT would fail if the waiting refresh held a candidate
            # first, forming the other half of the application/FK deadlock.
            await observer.execute(select(ChapterVariant.id).where(ChapterVariant.id == existing_id)
                                   .with_for_update(nowait=True))
            await observer.rollback()
            await owner.commit()
            await pending
            await worker.commit()
        finally:
            if not pending.done(): pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
