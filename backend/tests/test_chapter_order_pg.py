"""Real PostgreSQL leases, concurrent moves and transactional source notifications."""
import asyncio
from copy import deepcopy

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import select, text, update

from app.api.chapter_order import structure_write
from app.models.project import Chapter, Outline
from app.services.chapter_order import chapter_order_version, lock_chapter_structure, swap_adjacent_chapters
from app.services.production.locking import project_worker_lock
from tests.test_pipeline_control import runtime_db
from tests.test_foreshadow_review_retention import migration
from app.api.outlines import update_outline, OutlineUpdate
from app.services.outline_order import REF


async def pair(r):
    async with r.sessions() as db:
        first = await db.get(Chapter, r.chapter)
        second = Chapter(volume_id=first.volume_id, chapter_idx=2, title='第二章', content_text='第二章正文。')
        db.add(second); await db.flush()
        ids = first.id, second.id
        await db.commit()
        return ids


async def versions(r, ids):
    async with r.sessions() as db:
        rows = (await db.execute(select(*Chapter.__table__.columns).where(Chapter.id.in_(ids)))).mappings().all()
        return {row['id']:chapter_order_version(row) for row in rows}


@pytest.mark.asyncio
async def test_concurrent_moves_have_one_winner_and_one_conflict(runtime_db):
    r = runtime_db; ids = await pair(r); original = await versions(r, ids)
    async def move():
        async with r.sessions() as db:
            try:
                async with structure_write(db, r.project):
                    await swap_adjacent_chapters(db, r.project, *ids, original[ids[0]], original[ids[1]])
                return 200
            except HTTPException as exc:
                return exc.status_code
    assert sorted(await asyncio.gather(move(),move())) == [200,409]
    async with r.sessions() as db:
        rows = (await db.execute(select(Chapter.id,Chapter.chapter_idx,Chapter.global_idx)
            .where(Chapter.id.in_(ids)).order_by(Chapter.chapter_idx))).all()
        assert [row.id for row in rows] == [ids[1],ids[0]]
        assert [row.global_idx for row in rows] == [1,2]


@pytest.mark.asyncio
async def test_prose_worker_lease_excludes_structural_moves(runtime_db):
    r = runtime_db
    async with project_worker_lock(r.engine,r.project) as lease:
        assert lease
        async with r.sessions() as db:
            with pytest.raises(HTTPException) as exc:
                async with structure_write(db,r.project):
                    pytest.fail('Must not enter mutation while a writer owns the project')
            assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_swapped_sources_and_notifications_commit_or_rollback_together(runtime_db):
    r = runtime_db; ids = await pair(r)
    step = migration('a1001925_entity_source_outbox')
    def install(connection):
        with Operations.context(MigrationContext.configure(connection)):
            step.upgrade()
    async with r.engine.begin() as connection:
        await connection.run_sync(install)
    async with r.sessions() as db:
        await db.execute(update(Chapter).where(Chapter.id.in_(ids)).values(status='completed'))
        await db.commit()
    async def notifications():
        async with r.sessions() as db:
            return dict((await db.execute(text('SELECT chapter_id, revision FROM entity_source_outbox'))).all())
    before = await notifications(); original = await versions(r,ids)
    async with r.sessions() as db:
        await lock_chapter_structure(db,r.project)
        await swap_adjacent_chapters(db,r.project,*ids,original[ids[0]],original[ids[1]])
        await db.rollback()
    assert await notifications() == before
    assert await versions(r,ids) == original
    async with r.sessions() as db:
        async with structure_write(db,r.project):
            await swap_adjacent_chapters(db,r.project,*ids,original[ids[0]],original[ids[1]])
    after = await notifications()
    assert set(after) == set(ids)
    assert all(after[cid] > before[cid] for cid in ids)


@pytest.mark.asyncio
async def test_outline_bindings_and_numbers_rollback_with_the_actual_pg_move(runtime_db):
    r = runtime_db; ids = await pair(r)
    async with r.sessions() as db:
        chapters = list((await db.scalars(select(Chapter).where(Chapter.id.in_(ids))
            .order_by(Chapter.chapter_idx))).all())
        stubs = [{'chapter_idx': chapter.chapter_idx, 'summary': f'原计划{index}'} for index, chapter in enumerate(chapters)]
        for chapter, stub in zip(chapters, stubs):
            chapter.outline_json = deepcopy(stub)
        outline = Outline(project_id=r.project, level='volume', content_json={
            'volume_id': str(chapters[0].volume_id), 'volume_idx': 1, 'chapter_summaries': stubs})
        db.add(outline); await db.commit()
        oid, original = outline.id, deepcopy(outline.content_json)
    expected = await versions(r, ids)
    async with r.sessions() as db:
        await lock_chapter_structure(db, r.project)
        await swap_adjacent_chapters(db, r.project, *ids, expected[ids[0]], expected[ids[1]])
        moved = await db.get(Outline, oid)
        assert moved.content_json['chapter_summaries'][0][REF] == str(ids[1])
        await db.rollback()
    async with r.sessions() as db:
        assert (await db.get(Outline, oid)).content_json == original
    assert await versions(r, ids) == expected


@pytest.mark.asyncio
async def test_outline_editor_loaded_before_move_cannot_save_over_new_associations(runtime_db):
    r = runtime_db; ids = await pair(r)
    async with r.sessions() as db:
        first = await db.get(Chapter, ids[0]); second = await db.get(Chapter, ids[1])
        first.outline_json = {'chapter_idx': 1, 'summary': '还信'}
        second.outline_json = {'chapter_idx': 2, 'summary': '过桥'}
        original = {'volume_id': str(first.volume_id), 'volume_idx': 1,
            'chapter_summaries': [deepcopy(first.outline_json), deepcopy(second.outline_json)]}
        outline = Outline(project_id=r.project, level='volume', content_json=original)
        db.add(outline); await db.commit(); oid = outline.id
    expected = await versions(r, ids)
    async with r.sessions() as db:
        async with structure_write(db, r.project):
            await swap_adjacent_chapters(db, r.project, *ids, expected[ids[0]], expected[ids[1]])
    async with r.sessions() as db:
        with pytest.raises(HTTPException) as exc:
            await update_outline(str(r.project), str(oid), OutlineUpdate(
                expected_content_json=original, content_json={**original, 'title': '迟到的旧稿修改'}), db)
        assert exc.value.status_code == 409
        await db.rollback()
    async with r.sessions() as db:
        content = (await db.get(Outline, oid)).content_json
        assert content['chapter_summaries'][0][REF] == str(ids[1])
        assert content.get('title') != '迟到的旧稿修改'
