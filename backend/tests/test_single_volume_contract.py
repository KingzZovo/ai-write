import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outline_generator import OutlineGenerator
from tests.support.volume_meta import valid_volume_meta


def complete():
    meta = valid_volume_meta(2)
    meta['chapter_summaries'] = [
        {'chapter_idx': i, 'title': f'桥头{i}', 'summary': '完成交信', 'key_events': ['抛绳', '交信']}
        for i in (1, 2)
    ]
    return meta


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_single_volume_validates_and_repairs_before_delivery(stream):
    good = complete()
    bad = copy.deepcopy(good)
    bad['chapter_summaries'][1]['chapter_idx'] = 1
    calls, closed = [], []
    def response(kwargs):
        calls.append(copy.deepcopy(kwargs))
        assert kwargs['task_type'] == 'outline_volume'
        assert '2-2' in kwargs['messages'][1]['content']
        return json.dumps(bad if len(calls) == 1 else good)
    async def generate(**kwargs): return SimpleNamespace(text=response(kwargs))
    async def generate_stream(**kwargs):
        text = response(kwargs)
        try:
            yield text[:30]
            yield text[30:]
        finally:
            closed.append(True)
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=(2, 2))
    obj.router = SimpleNamespace(generate=generate, generate_stream=generate_stream)
    result = await obj.generate_volume_outline({}, 1, staged=False, stream=stream)
    if stream:
        parts = [part async for part in result]
        assert len(parts) == 1
        result = json.loads(parts[0])
        assert len(closed) == 2
    assert result == good
    assert len(calls) == 2
    assert '章号错误' in calls[1]['messages'][-1]['content']
    assert json.loads(calls[1]['messages'][-2]['content']) == bad


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('defect', ['missing_meta', 'wrong_count', 'missing_summary', 'out_of_range'])
async def test_invalid_single_volume_is_never_delivered_as_valid(stream, defect):
    meta = complete()
    if defect == 'missing_meta': del meta['volume_end_state']
    elif defect == 'wrong_count': meta['chapter_count'] = 3
    elif defect == 'missing_summary': del meta['chapter_summaries'][0]['summary']
    pref = (4, 4) if defect == 'out_of_range' else None
    calls = []
    async def generate(**kw):
        calls.append(kw)
        return SimpleNamespace(text=json.dumps(meta))
    async def generate_stream(**kw):
        calls.append(kw)
        yield json.dumps(meta)
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=pref)
    obj.router = SimpleNamespace(generate=generate, generate_stream=generate_stream)
    result = await obj.generate_volume_outline({}, 1, staged=False, stream=stream)
    if stream:
        with pytest.raises(ValueError):
            await anext(result)
    else:
        assert result['_parse_error']
        assert 'chapter_summaries' not in result
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_single_stream_closes_without_emitting_partial_json():
    closed = []
    async def stream(**kwargs):
        try:
            yield '{"title":"partial'
            raise RuntimeError('connection interrupted')
        finally:
            closed.append(True)
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=None)
    obj.router = SimpleNamespace(generate_stream=stream)
    result = await obj.generate_volume_outline({}, 1, staged=False, stream=True)
    with pytest.raises(ValueError, match='connection interrupted'):
        await anext(result)
    assert closed == [True]


@pytest.mark.asyncio
async def test_cancellation_closes_single_volume_stream():
    started, closed = asyncio.Event(), asyncio.Event()
    async def stream(**kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
            yield ''
        finally:
            closed.set()
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=None)
    obj.router = SimpleNamespace(generate_stream=stream)
    result = await obj.generate_volume_outline({}, 1, staged=False, stream=True)
    task = asyncio.create_task(anext(result))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set()
