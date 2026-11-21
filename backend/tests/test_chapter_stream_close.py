from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from app.services import chapter_generator, scene_orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['chapter','scene','orchestrated'])
async def test_chapter_services_close_prompt_stream_without_starting_next_scene(monkeypatch, mode):
    events = []
    async def prompt(**kwargs):
        events.append('opened')
        try:
            yield '首段。'
            yield '后续。'
        finally: events.append('closed')
    pack = NS(to_messages=lambda instruction: [], to_system_prompt=lambda: '背景', character_cards=[],
              resolve_cognition=AsyncMock())
    @asynccontextmanager
    async def sessions(db): yield db
    module = chapter_generator if mode == 'chapter' else scene_orchestrator
    monkeypatch.setattr(module, 'stream_text_prompt', prompt)
    monkeypatch.setattr(module, 'generation_context_session', sessions)
    monkeypatch.setattr(module, 'ContextPackBuilder', lambda **kw: NS(build=AsyncMock(return_value=pack)))
    kwargs = dict(project_id='p', chapter_id='c', volume_id='v', chapter_idx=1, db=None)
    if mode == 'chapter':
        service = module.ChapterGenerator()
        monkeypatch.setattr(service, '_assert_outline_chain_ready', AsyncMock())
        monkeypatch.setattr(service, '_collect_rag_hits', lambda pack: [])
        stream = service.generate_stream(**kwargs)
    else:
        service = module.SceneOrchestrator()
        scenes = [module.SceneBrief(idx=i,title='场景',brief='交还钥匙',target_words=1000) for i in (1,2)]
        if mode == 'scene':
            stream = service.write_scene_stream(scene=scenes[0], pack=pack, prior_scenes_summary='', db=None, project_id='p',chapter_id='c')
        else:
            monkeypatch.setattr(service, '_assert_outline_chain_ready', AsyncMock())
            monkeypatch.setattr(service, 'plan_scenes', AsyncMock(return_value=scenes))
            stream = service.orchestrate_chapter_stream(**kwargs)
    assert await anext(stream) == '首段。'
    await stream.aclose()
    assert events == ['opened','closed']
