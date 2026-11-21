from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from app.services.context_errors import RequiredContextUnavailable
from app.services.context_pack import ContextPack, ContextPackBuilder
from app.services.generation_errors import generation_error_payload


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('private source data'), TypeError('invalid row'), SQLAlchemyError('database failed')])
async def test_proximity_read_failures_cannot_return_a_valid_empty_pack(error):
    db = NS(scalar=AsyncMock(return_value=NS(id=uuid4(), volume_idx=1)),
            scalars=AsyncMock(return_value=NS(all=lambda: [NS(id=uuid4(), chapter_idx=2)])),
            execute=AsyncMock(side_effect=error))
    builder = ContextPackBuilder(db=db)
    expected = SQLAlchemyError if isinstance(error, SQLAlchemyError) else RequiredContextUnavailable
    with pytest.raises(expected) as caught:
        await builder._build_proximity(ContextPack(), uuid4(), uuid4(), 2)
    if expected is RequiredContextUnavailable:
        assert caught.value.__cause__ is error
        public = generation_error_payload(caught.value, 'chapter')
        assert public['code'] == 'chapter_context_unavailable'
        assert '前文衔接材料' in public['error'] and 'private' not in str(public)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_direct_generation_does_not_call_model_after_context_failure(monkeypatch, streaming):
    from app.services import chapter_generator as module
    @asynccontextmanager
    async def context(db): yield db
    monkeypatch.setattr(module, 'generation_context_session', context)
    monkeypatch.setattr(module.ChapterGenerator, '_assert_outline_chain_ready', AsyncMock())
    monkeypatch.setattr(module, 'ContextPackBuilder', lambda **kw: NS(
        build=AsyncMock(side_effect=RequiredContextUnavailable('continuity unavailable'))))
    generate = AsyncMock()
    stream = MagicMock()
    monkeypatch.setattr(module, 'run_text_prompt', generate)
    monkeypatch.setattr(module, 'stream_text_prompt', stream)
    kwargs = dict(project_id=uuid4(), volume_id=uuid4(), chapter_idx=2, db=None)
    with pytest.raises(RequiredContextUnavailable):
        if streaming:
            async for _ in module.ChapterGenerator().generate_stream(**kwargs):
                pytest.fail('must not emit invented prose')
        else:
            await module.ChapterGenerator().generate(**kwargs)
    generate.assert_not_called()
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_scene_generation_does_not_plan_from_unavailable_context(monkeypatch):
    from app.services import scene_orchestrator as module
    @asynccontextmanager
    async def context(db): yield db
    monkeypatch.setattr(module, 'generation_context_session', context)
    monkeypatch.setattr(module.SceneOrchestrator, '_assert_outline_chain_ready', AsyncMock())
    monkeypatch.setattr(module, 'ContextPackBuilder', lambda **kw: NS(
        build=AsyncMock(side_effect=RequiredContextUnavailable('continuity unavailable'))))
    plan = AsyncMock()
    write = MagicMock()
    monkeypatch.setattr(module.SceneOrchestrator, 'plan_scenes', plan)
    monkeypatch.setattr(module.SceneOrchestrator, 'write_scene_stream', write)
    with pytest.raises(RequiredContextUnavailable):
        async for _ in module.SceneOrchestrator().orchestrate_chapter_stream(
            project_id=uuid4(), volume_id=uuid4(), chapter_idx=2, db=None,
        ):
            pytest.fail('must not emit invented scenes')
    plan.assert_not_called()
    write.assert_not_called()
