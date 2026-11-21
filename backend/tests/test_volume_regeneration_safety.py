import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, event
from sqlalchemy.orm import Session

from tests.test_pipeline_control import runtime_db
from app.api import volumes as api
from app.db import session
from app.models.project import Chapter, ChapterVersion, ChapterEvaluation, Outline, Volume
from app.services.outline_generator import OutlineGenerator


@pytest_asyncio.fixture
async def fixture(runtime_db, monkeypatch):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda sync: [table.create(sync, checkfirst=True) for table in (
            Outline.__table__, ChapterVersion.__table__, ChapterEvaluation.__table__)])
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.chapter)
        r.volume = chapter.volume_id
        volume = await db.get(Volume, r.volume); volume.target_word_count = 5400
        book = Outline(project_id=r.project, level='book', is_confirmed=1, content_json={'raw_text': '全书计划。'})
        db.add(book); await db.flush(); r.book = book.id
        old = Outline(project_id=r.project, level='volume', parent_id=book.id, content_json={'volume_idx': 1, 'title': '原卷纲'})
        version = ChapterVersion(chapter_id=r.chapter, content_text='历史正文。')
        db.add_all([old, version]); await db.commit(); r.old_outline, r.version = old.id, version.id
    monkeypatch.setattr(api, 'async_session_factory', r.sessions)
    monkeypatch.setattr(session._state, 'engine', r.engine)
    monkeypatch.setattr('app.services.compass_service.update_on_new_volume', AsyncMock())
    return r


def payload():
    return {'volume_idx': 1, 'title': '新卷', 'core_conflict': '把承诺完成。', 'chapter_count': 3,
            'chapter_summaries': [{'chapter_idx': i, 'title': f'新章{i}', 'summary': '具体行动。',
                                    'key_events': ['清点', '说明']} for i in range(1, 4)]}


async def run(r):
    async with r.sessions() as db:
        response = await api.regenerate_volume(str(r.project), str(r.volume), db)
        return ''.join([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['model', 'empty', 'json', 'count', 'wrong_volume', 'commit', None])
async def test_regeneration_replaces_only_after_success(fixture, monkeypatch, failure):
    r = fixture
    async def generate(self, **kwargs):
        assert self.project_id == str(r.project)
        assert kwargs['book_outline'] == {'raw_text': '全书计划。'}
        async with r.sessions() as db:
            assert (await db.get(Chapter, r.chapter)).content_text == '原稿。'
            assert await db.get(Outline, r.old_outline)
            assert await db.get(ChapterVersion, r.version)
        async def stream():
            if failure == 'model': raise RuntimeError('private provider error')
            if failure == 'empty': return
            if failure == 'json':
                yield json.dumps(payload())[:-2]; return
            data = payload()
            if failure == 'count': data['chapter_count'] = 4
            if failure == 'wrong_volume': data['volume_idx'] = 2
            yield json.dumps(data)
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_volume_outline', generate)
    def fail_commit(db):
        if failure == 'commit':
            raise RuntimeError('simulated commit failure')
    event.listen(Session, 'before_commit', fail_commit)
    try:
        events = await run(r)
    finally:
        event.remove(Session, 'before_commit', fail_commit)
    async with r.sessions() as db:
        if failure:
            assert '"status": "failed"' in events and '"status": "done"' not in events
            assert 'private provider error' not in events
            assert (await db.get(Chapter, r.chapter)).content_text == '原稿。'
            assert await db.get(Outline, r.old_outline)
            assert await db.get(ChapterVersion, r.version)
        else:
            assert '"status": "done"' in events
            assert await db.get(Chapter, r.chapter) is None
            rows = list((await db.scalars(select(Chapter).where(Chapter.volume_id == r.volume))).all())
            assert len(rows) == 3 and all(c.target_word_count == 1800 for c in rows)
            assert (await db.get(Volume, r.volume)).title == '新卷'
            assert await db.get(Outline, r.old_outline) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['chapter', 'new_chapter', 'book', 'volume', 'outline', 'version'])
async def test_generation_rejects_midflight_edits(fixture, monkeypatch, change):
    r = fixture
    async def generate(self, **kwargs):
        async with r.sessions() as db:
            if change == 'chapter': (await db.get(Chapter, r.chapter)).content_text = '作者新稿。'
            if change == 'new_chapter': db.add(Chapter(volume_id=r.volume, chapter_idx=2, title='作者新章'))
            if change == 'version': (await db.get(ChapterVersion, r.version)).content_text = '作者新历史稿。'
            if change == 'outline': (await db.get(Outline, r.old_outline)).content_json = {'volume_idx': 1, 'title': '作者新卷纲'}
            if change == 'book': (await db.get(Outline, r.book)).content_json = {'raw_text': '作者新大纲。'}
            if change == 'volume': (await db.get(Volume, r.volume)).target_word_count = 6000
            await db.commit()
        async def stream(): yield json.dumps(payload())
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_volume_outline', generate)
    events = await run(r)
    assert '"status": "failed"' in events and '"status": "done"' not in events
    async with r.sessions() as db:
        ch = await db.get(Chapter, r.chapter)
        assert ch.content_text == ('作者新稿。' if change == 'chapter' else '原稿。')
        assert await db.get(Outline, r.old_outline)
        assert await db.get(ChapterVersion, r.version)


@pytest.mark.asyncio
async def test_cancelled_stream_keeps_old_content(fixture, monkeypatch):
    r = fixture; entered = asyncio.Event()
    async def generate(self, **kwargs):
        async def stream():
            entered.set(); await asyncio.Event().wait()
            yield ''
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_volume_outline', generate)
    running = asyncio.create_task(run(r)); await asyncio.wait_for(entered.wait(), 3)
    running.cancel()
    with pytest.raises(asyncio.CancelledError): await running
    async with r.sessions() as db:
        assert (await db.get(Chapter, r.chapter)).content_text == '原稿。'
        assert await db.get(Outline, r.old_outline)


@pytest.mark.asyncio
async def test_parallel_regenerations_share_project_exclusion(fixture, monkeypatch):
    r = fixture; entered = asyncio.Event(); release = asyncio.Event(); calls = []
    async def generate(self, **kwargs):
        calls.append(True)
        async def stream():
            entered.set(); await release.wait(); yield json.dumps(payload())
        return stream()
    monkeypatch.setattr(OutlineGenerator, 'generate_volume_outline', generate)
    first = asyncio.create_task(run(r)); await asyncio.wait_for(entered.wait(), 3)
    try:
        second = await asyncio.wait_for(run(r), 3)
        assert '"status": "failed"' in second
        assert len(calls) == 1
    finally:
        release.set()
        events = await first
    assert '"status": "done"' in events
