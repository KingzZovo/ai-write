"""Offline source/save and stream checks. These do not prove PostgreSQL isolation."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from app.models.project import Outline, Project
from app.services import book_outline_save as service

TEXT = '林禾在关店前修好陈婶的家谱，交还后收起母亲的菜谱。'
PAYLOAD = {'volume_plan': [{'idx': 1, 'est_chapters': 3}], 'structured': {'premise': '修书与告别'}}


def sources():
    project = Project(id=uuid4(), title='修补以后', target_word_count=5400, settings_json={})
    old = Outline(id=uuid4(), project_id=project.id, level='book', content_json={'raw_text': '作者旧稿'}, version=3, is_confirmed=1)
    books = [old]
    db = SimpleNamespace(scalar=AsyncMock(return_value=project),
        scalars=AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(all=lambda: books)),
        add=Mock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    return project, books, db


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'project', 'book', 'confirmation', 'new_book', 'deleted'])
async def test_rechecks_sources_and_retains_existing_candidates(change):
    project, books, db = sources()
    snapshot = await service.snapshot_book_outline(db, project.id)
    if change == 'project': project.settings_json = {'target_chapter_words': 2000}
    if change == 'book': books[0].content_json['raw_text'] = '作者修改'
    if change == 'confirmation': books[0].is_confirmed = 0
    if change == 'new_book': books.append(Outline(id=uuid4(), project_id=project.id, level='book', content_json={'raw_text': '新候选'}, version=4))
    if change == 'deleted': db.scalar.return_value = None
    before = deepcopy(books[0].content_json)
    if change == 'none':
        outline = await service.save_book_outline(db, snapshot, TEXT, PAYLOAD)
        assert outline.version == 4 and outline.is_confirmed == 0 and outline.parent_id is None
        assert outline.content_json['raw_text'] == TEXT
        db.flush.assert_awaited_once()
    else:
        with pytest.raises(ValueError): await service.save_book_outline(db, snapshot, TEXT, PAYLOAD)
        db.add.assert_not_called(); db.flush.assert_not_awaited()
    assert books[0].content_json == before
    assert db.scalar.call_args.args[0]._for_update_arg is not None
    db.commit.assert_not_awaited()


@pytest.mark.parametrize('text,payload', [('', None), ('   ', None), (TEXT, {'structured': ['bad']}), (TEXT, {}), (TEXT + '<volume-plan>broken</volume-plan>', None),
    (TEXT, {'volume_plan': 'bad'}), (TEXT, {'volume_plan': [{'idx': 1, 'est_chapters': 0}]}),
    (TEXT, {'volume_plan': [{'idx': 1, 'est_chapters': 2}, {'idx': 1, 'est_chapters': 2}]})])
def test_bad_book_payload_is_not_saved(text, payload):
    with pytest.raises(ValueError): service.book_content(text, payload)


def test_content_keeps_author_text_and_canonical_plan():
    payload = deepcopy(PAYLOAD)
    payload['structured'].update(raw_text='模型重复正文', volume_plan=[{'idx': 99}])
    saved = service.book_content(TEXT, payload)
    assert saved['raw_text'] == TEXT and saved['volume_plan'] == PAYLOAD['volume_plan']
    payload['volume_plan'][0]['idx'] = 10
    assert saved['volume_plan'][0]['idx'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['success', 'legacy_success', 'legacy_empty', 'missing_done', 'empty', 'stage_error', 'source_changed', 'commit_failure', 'cancel_save'])
async def test_actual_book_stream_requires_committed_unchanged_source(monkeypatch, mode):
    from app.api import generate as api
    from app.db import session
    project, books, save_db = sources()
    request_db = SimpleNamespace(get=AsyncMock(side_effect=lambda model, *a: project if model is Project else None),
        scalar=save_db.scalar, scalars=save_db.scalars, rollback=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    monkeypatch.setattr('app.services.model_router.get_model_router_async', AsyncMock())
    monkeypatch.setattr('app.services.style_runtime.resolve_style_prompt', AsyncMock(return_value=''))
    monkeypatch.setattr(api.OutlineGenerator, '__init__', lambda self, **kw: None)
    in_guard = False
    drained = False
    async def guard(engine, factory, pid, source, **kw):
        nonlocal in_guard
        assert kw['metadata_only'] is True
        in_guard = True
        try:
            async for frame in source(): yield frame
        finally:
            if mode == 'cancel_save': assert drained
            in_guard = False
    monkeypatch.setattr('app.services.production.writer.guarded_stream', guard)
    async def generate(self, **kw):
        assert in_guard and request_db.rollback.await_count == 1
        if mode == 'source_changed': project.title = '作者已修改书名'
        async def stream():
            if mode.startswith('legacy_'):
                if mode == 'legacy_success': yield TEXT + '<volume-plan>' + json.dumps(PAYLOAD['volume_plan']) + '</volume-plan>'
                return
            yield {'event': 'stage_chunk', 'delta': TEXT}
            if mode != 'missing_done': yield {'event': 'done', 'full_outline': '' if mode == 'empty' else TEXT, **PAYLOAD}
            if mode == 'stage_error': yield {'event': 'error', 'error': 'stage failed'}
        return stream()
    monkeypatch.setattr(api.OutlineGenerator, 'generate_book_outline', generate)
    class SaveContext:
        async def __aenter__(self): return save_db
        async def __aexit__(self, *exc):
            nonlocal drained
            drained = True
    monkeypatch.setattr(session, 'async_session_factory', lambda: SaveContext())
    entered = asyncio.Event()
    if mode == 'commit_failure': save_db.commit.side_effect = RuntimeError('commit failed')
    if mode == 'cancel_save':
        async def pause():
            entered.set()
            await asyncio.Event().wait()
        save_db.flush.side_effect = pause
    response = await api.generate_outline(api.GenerateOutlineRequest(project_id=str(project.id), level='book', staged_stream=not mode.startswith('legacy_')), db=request_db)
    async def consume(): return ''.join([frame async for frame in response.body_iterator])
    if mode == 'cancel_save':
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert drained and not in_guard
        save_db.commit.assert_not_awaited()
    else:
        frames = await consume()
        assert ('"status": "saved"' in frames) == (mode in ('success', 'legacy_success'))
        assert ('"status": "completed"' in frames) == (mode in ('success', 'legacy_success'))
        if mode in ('missing_done', 'legacy_empty', 'empty', 'stage_error', 'source_changed'):
            save_db.add.assert_not_called()
    assert books[0].content_json == {'raw_text': '作者旧稿'}
