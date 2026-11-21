import asyncio
from types import SimpleNamespace as NS
import pytest
from app.services.outline_generator import OutlineGenerator


@pytest.mark.asyncio
async def test_closing_during_skeleton_closes_model_before_other_stages():
    events = []
    async def model(**kwargs):
        events.append(kwargs['task_type'])
        try:
            yield '骨架片段'
            await asyncio.Event().wait()
        finally: events.append('closed')
    generator = OutlineGenerator()
    generator.router = NS(generate_stream=model)
    stream = generator._generate_book_outline_staged_stream('创意')
    assert (await anext(stream))['event'] == 'stage_start'
    assert (await anext(stream))['event'] == 'stage_chunk'
    await stream.aclose()
    assert events == ['outline_book_skeleton','closed']


@pytest.mark.asyncio
async def test_parallel_stages_are_cancelled_and_joined_on_close(monkeypatch):
    opened = {name: asyncio.Event() for name in ['outline_book_characters','outline_book_world']}
    closed = []
    async def model(**kwargs):
        name = kwargs['task_type']
        if name == 'outline_book_skeleton':
            yield '骨架'
            return
        try:
            opened[name].set()
            yield '阶段正文'
            await asyncio.Event().wait()
        finally: closed.append(name)
    generator = OutlineGenerator()
    generator.router = NS(generate_stream=model)
    monkeypatch.setattr(generator, '_validate_book_stage', lambda *args: None)
    stream = generator._generate_book_outline_staged_stream('创意')
    try:
        while True:
            event = await anext(stream)
            if event.get('stage') in ('B','C'): break
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in opened.values())), 1)
    finally:
        await stream.aclose()
    assert set(closed) == set(opened)
