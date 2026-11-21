import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from tests.test_volume_creation import creation
from tests.test_pipeline_control import runtime_db
from app.models.project import Chapter, Outline, Project, Volume
from app.services.chapter_outline_save import snapshot_chapter_outline, save_chapter_outline


@pytest_asyncio.fixture
async def planned(creation):
    r = creation
    async with r.sessions() as db:
        volume = Volume(project_id=r.pid, volume_idx=1, title='作者分卷', target_word_count=5400)
        db.add(volume)
        await db.flush()
        parent = Outline(project_id=r.pid, parent_id=r.bid, level='volume', is_confirmed=1,
                         content_json={'volume_id': str(volume.id), 'volume_idx': 1,
                                       'chapter_count': 3, 'title': '作者卷纲', 'core_conflict': '按约交还信件'})
        chapter = Chapter(volume_id=volume.id, chapter_idx=2, title='作者章名', content_text='作者正文，原样保留。',
                          target_word_count=1800, outline_json={'chapter_idx': 2, 'summary': '原细纲', 'key_events': ['原安排']})
        previous = Chapter(volume_id=volume.id, chapter_idx=1, title='前章', content_text='尚未交付。')
        db.add_all([parent, chapter, previous])
        await db.flush()
        old = Outline(project_id=r.pid, parent_id=parent.id, level='chapter', version=3,
                      content_json={'chapter_id': str(chapter.id), 'chapter_idx': 2, 'summary': '原细纲'})
        db.add(old)
        await db.commit()
        r.vid, r.parent, r.cid, r.previous, r.old = volume.id, parent.id, chapter.id, previous.id, old.id
    return r


def payload():
    return {'chapter_idx': 2, 'title': '交还信件', 'summary': '阿青渡河，将信件交还给收件人。',
            'key_events': ['渡河', '交还信件'], 'word_count_target': 9000}


async def snapshot(r):
    async with r.sessions() as db:
        return await snapshot_chapter_outline(db, r.pid, r.parent, 2)


async def check_original(r):
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.cid)
        assert chapter.content_text == '作者正文，原样保留。'
        assert chapter.title == '作者章名'
        assert chapter.outline_json['summary'] == '原细纲'
        outlines = (await db.scalars(select(Outline).where(Outline.project_id == r.pid, Outline.level == 'chapter'))).all()
        assert len(outlines) == 1 and outlines[0].id == r.old


@pytest.mark.asyncio
async def test_saves_plan_and_chapter_together_preserving_prose_and_history(planned):
    r = planned
    source = await snapshot(r)
    async with r.sessions() as db:
        outline, title = await save_chapter_outline(db, source, json.dumps(payload()))
        await db.commit()
        chapter = await db.get(Chapter, r.cid)
        assert title == chapter.title == '交还信件'
        assert outline.version == 4 and await db.get(Outline, r.old)
        assert outline.content_json == chapter.outline_json
        assert chapter.outline_json['word_count_target'] == chapter.target_word_count == 1800
        assert chapter.outline_json['chapter_id'] == str(r.cid)
        assert chapter.content_text == '作者正文，原样保留。'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['truncated', 'empty_events', 'wrong_index', 'bad_changes'])
async def test_invalid_generation_cannot_save_partial_plan(planned, kind):
    r = planned
    source = await snapshot(r)
    data = payload()
    if kind == 'empty_events': data['key_events'] = []
    if kind == 'wrong_index': data['chapter_idx'] = 1
    if kind == 'bad_changes': data['state_changes'] = {'characters': [{'name': '阿青'}]}
    raw = '{"summary":' if kind == 'truncated' else json.dumps(data)
    async with r.sessions() as db:
        with pytest.raises(ValueError): await save_chapter_outline(db, source, raw)
        await db.rollback()
    await check_original(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['project', 'book', 'parent', 'volume', 'chapter', 'previous', 'outline'])
async def test_edits_during_generation_are_not_overwritten(planned, kind):
    r = planned
    source = await snapshot(r)
    model, identifier, field, value = {
        'project': (Project, r.pid, 'target_word_count', 6000),
        'book': (Outline, r.bid, 'content_json', {'raw_text': '作者新书纲'}),
        'parent': (Outline, r.parent, 'is_confirmed', 0),
        'volume': (Volume, r.vid, 'title', '作者新卷名'),
        'chapter': (Chapter, r.cid, 'outline_json', {'summary': '作者新细纲'}),
        'previous': (Chapter, r.previous, 'content_text', '前章已经交付。'),
        'outline': (Outline, r.old, 'content_json', {'summary': '作者修改历史细纲'}),
    }[kind]
    async with r.sessions() as db:
        setattr(await db.get(model, identifier), field, value)
        await db.commit()
    async with r.sessions() as db:
        with pytest.raises(ValueError): await save_chapter_outline(db, source, json.dumps(payload()))
        await db.rollback()
        assert getattr(await db.get(model, identifier), field) == value
        assert (await db.get(Chapter, r.cid)).content_text == '作者正文，原样保留。'
        assert len((await db.scalars(select(Outline).where(Outline.project_id == r.pid, Outline.level == 'chapter'))).all()) == 1


@pytest.mark.asyncio
async def test_concurrent_saves_have_only_one_winner(planned):
    r = planned
    source = await snapshot(r)
    async def save():
        async with r.sessions() as db:
            try:
                await save_chapter_outline(db, source, json.dumps(payload()))
                await db.commit()
                return True
            except ValueError:
                await db.rollback()
                return False
    assert sorted(await asyncio.gather(save(), save())) == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['wrong_project', 'unconfirmed', 'duplicate_chapter', 'missing_chapter'])
async def test_invalid_target_is_rejected_before_generation(planned, kind):
    r = planned
    async with r.sessions() as db:
        if kind == 'wrong_project': (await db.get(Outline, r.parent)).project_id = r.project
        elif kind == 'unconfirmed': (await db.get(Outline, r.parent)).is_confirmed = 0
        elif kind == 'duplicate_chapter': db.add(Chapter(volume_id=r.vid, chapter_idx=2, title='重复'))
        else: await db.execute(delete(Chapter).where(Chapter.id == r.cid))
        await db.commit()
    with pytest.raises(ValueError): await snapshot(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'bad_json', 'commit_failure', 'cancel_save'])
async def test_actual_stream_only_confirms_atomic_success(planned, monkeypatch, mode):
    from app.api import generate as api
    from app.db import session
    r = planned
    monkeypatch.setattr(session._state, 'sessionmaker', r.sessions)
    monkeypatch.setattr(session._state, 'engine', r.engine)
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock())
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    monkeypatch.setattr('app.services.foreshadow_lifecycle.chapter_global_idx', AsyncMock(return_value=2))
    async def generate(self, **kwargs):
        assert kwargs['target_word_count'] == 1800
        assert kwargs['book_outline']['volume_plan'] == [{'idx': 1, 'est_chapters': 3}]
        assert kwargs['volume_outline']['volume_id'] == str(r.vid)
        async def stream(): yield '{bad' if mode == 'bad_json' else json.dumps(payload())
        return stream()
    monkeypatch.setattr(api.OutlineGenerator, 'generate_chapter_outline', generate)
    entered_save = asyncio.Event()
    if mode == 'cancel_save':
        async def paused_save(db, source, raw):
            result = await save_chapter_outline(db, source, raw)
            entered_save.set()
            await asyncio.Event().wait()
            return result
        monkeypatch.setattr('app.services.chapter_outline_save.save_chapter_outline', paused_save)
    def fail(session):
        if mode == 'commit_failure': raise RuntimeError('simulated commit failure')
    event.listen(Session, 'before_commit', fail)
    try:
        async with r.sessions() as db:
            response = await api.generate_outline(api.GenerateOutlineRequest(project_id=str(r.pid), level='chapter',
                parent_outline_id=str(r.parent), chapter_idx=2), db=db)
            async def consume(): return ''.join([part async for part in response.body_iterator])
            if mode == 'cancel_save':
                task = asyncio.create_task(consume())
                await asyncio.wait_for(entered_save.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
                frames = ''
            else: frames = await consume()
    finally: event.remove(Session, 'before_commit', fail)
    assert ('"status": "saved"' in frames) == (mode == 'success')
    assert ('"status": "completed"' in frames) == (mode == 'success')
    if mode != 'success': await check_original(r)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['none', 'project', 'book', 'parent', 'volume', 'previous', 'new_preferred_book'])
async def test_expansion_checks_all_generation_sources(planned, monkeypatch, kind):
    from types import SimpleNamespace
    from app.services import chapter_outline_expander as expander, prompt_registry
    r = planned
    monkeypatch.setattr('app.services.foreshadow_lifecycle.load_active_foreshadows_for_context', AsyncMock(return_value=[]))
    async def generate(*args, **kwargs):
        async with r.sessions() as db:
            if kind == 'project': (await db.get(Project, r.pid)).settings_json = {'target_chapter_words': 2000}
            elif kind == 'book': (await db.get(Outline, r.bid)).content_json = {'raw_text': '作者改过的全书大纲'}
            elif kind == 'parent': (await db.get(Outline, r.parent)).content_json = {'volume_id': str(r.vid), 'title': '新卷纲'}
            elif kind == 'volume': (await db.get(Volume, r.vid)).title = '新卷名'
            elif kind == 'previous': (await db.get(Chapter, r.previous)).content_text = '前章已完成交付。'
            elif kind == 'new_preferred_book': db.add(Outline(project_id=r.pid, level='book', version=9, is_confirmed=1, content_json={'raw_text': '新确认的书纲'}))
            if kind != 'none': await db.commit()
        return SimpleNamespace(text=json.dumps(payload()))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', generate)
    async with r.sessions() as db:
        if kind == 'none':
            result = await expander.expand_chapter_outline(str(r.pid), str(r.cid), db)
            await db.commit()
            assert result['summary'] == payload()['summary']
            assert result['title'] == '作者章名'
        else:
            with pytest.raises(expander.ChapterOutlineExpandError, match='已修改'):
                await expander.expand_chapter_outline(str(r.pid), str(r.cid), db)
            await db.rollback()
    async with r.sessions() as db:
        chapter = await db.get(Chapter, r.cid)
        assert chapter.content_text == '作者正文，原样保留。'
        if kind != 'none': assert chapter.outline_json['summary'] == '原细纲'
