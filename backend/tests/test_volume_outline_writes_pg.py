"""Real PostgreSQL locks and receipts; deliberately unexecuted in offline mode."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api.volume_outlines import VolumeOutlineSave, save_volume_outline
from app.models.outline_write import VolumeOutlineWrite
from app.models.project import Chapter, Outline, Volume
from app.services.chapter_order import lock_chapter_structure
from app.services.volume_outline_writes import read_volume_outline
from tests.test_pipeline_control import runtime_db

pytestmark = pytest.mark.external_services


@pytest_asyncio.fixture
async def volume_book(runtime_db):
    r = runtime_db
    async with r.engine.begin() as connection:
        await connection.run_sync(lambda db: VolumeOutlineWrite.__table__.create(db))
    async with r.sessions() as db:
        r.vid = (await db.get(Chapter, r.chapter)).volume_id
        outline = Outline(project_id=r.project, level='volume', is_confirmed=1,
            content_json={'volume_id': str(r.vid), 'volume_idx': 1, 'raw_text': '作者原卷纲。'})
        db.add(outline); await db.commit()
        r.oid = outline.id
        r.source = await read_volume_outline(db, r.project, r.vid)
    return r


def request(r, text='作者当前改稿。'):
    return VolumeOutlineSave(request_id=uuid4(), expected_source=deepcopy(r.source), text=text)


async def write(r, body):
    async with r.sessions() as db:
        try:
            return 200, await save_volume_outline(r.project, r.vid, body, db)
        except HTTPException as exc:
            return exc.status_code, exc.detail


@pytest.mark.asyncio
async def test_same_volume_request_race_creates_one_history_and_replays_original(volume_book):
    r = volume_book
    body = request(r)
    results = await asyncio.gather(write(r, body), write(r, body))
    assert any(status == 200 for status, _ in results)
    assert all(status in {200, 409} for status, _ in results)
    first = next(value for status, value in results if status == 200)
    assert await write(r, body) == (200, first)
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VolumeOutlineWrite)) == 1
        assert (await db.get(Outline, r.oid)).content_json['raw_text'] == body.text


@pytest.mark.asyncio
async def test_different_author_requests_cannot_both_replace_the_same_original(volume_book):
    r = volume_book
    one, two = request(r, '甲窗口的新稿。'), request(r, '乙窗口的新稿。')
    results = await asyncio.gather(write(r, one), write(r, two))
    assert sorted(status for status, _ in results) == [200, 409]
    loser = two if results[0][0] == 200 else one
    assert (await write(r, loser))[0] == 409
    async with r.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VolumeOutlineWrite)) == 1


@pytest.mark.asyncio
async def test_volume_outline_writer_cannot_enter_during_a_structure_transaction(volume_book):
    r = volume_book
    async with r.sessions() as owner:
        await lock_chapter_structure(owner, r.project)
        assert (await write(r, request(r)))[0] == 409
        await owner.rollback()
    assert (await write(r, request(r)))[0] == 200


@pytest.mark.asyncio
async def test_lost_pg_commit_ack_replays_without_overwriting_a_later_author_edit(volume_book):
    r = volume_book
    body = request(r)
    async with r.sessions() as db:
        commit = db.commit
        async def lose_ack():
            await commit()
            raise RuntimeError('private acknowledgement diagnostic')
        db.commit = lose_ack
        with pytest.raises(HTTPException) as exc:
            await save_volume_outline(r.project, r.vid, body, db)
        assert exc.value.status_code == 503
    first = await write(r, body)
    assert first[0] == 200
    async with r.sessions() as db:
        current = await read_volume_outline(db, r.project, r.vid)
    later = VolumeOutlineSave(request_id=uuid4(), expected_source=current, text='后来的作者新稿。')
    assert (await write(r, later))[0] == 200
    assert await write(r, body) == first
    async with r.sessions() as db:
        assert (await db.get(Outline, r.oid)).content_json['raw_text'] == later.text


@pytest.mark.asyncio
async def test_stale_volume_title_and_confirmation_are_reloaded_under_pg_locks(volume_book):
    r = volume_book
    async with r.sessions() as stale:
        await stale.get(Volume, r.vid)
        await stale.get(Outline, r.oid)
        async with r.sessions() as author:
            (await author.get(Volume, r.vid)).title = '作者新卷名'
            (await author.get(Outline, r.oid)).is_confirmed = 0
            await author.commit()
        with pytest.raises(HTTPException) as exc:
            await save_volume_outline(r.project, r.vid, request(r), stale)
        assert exc.value.status_code == 409
