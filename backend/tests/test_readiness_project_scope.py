"""A ready outline in one book cannot authorize another book's chapter."""
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, func

from tests.test_pipeline_control import runtime_db
from app.models import Base
from app.models.project import Project, Volume, Chapter, Outline
from app.models.generation_task import GenerationTask
from app.services.outline_readiness import build_outline_readiness_report
from app.db.session import get_db


@pytest_asyncio.fixture
async def books(runtime_db):
    r = runtime_db
    async with r.engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Outline.__table__]))
    async with r.sessions() as db:
        own = await db.get(Chapter, r.chapter)
        own.outline_json = {'summary': '本书本章的大纲'}
        other = Project(title='另一部有完整大纲的测试小说')
        db.add(other)
        await db.flush()
        volume = Volume(project_id=other.id, volume_idx=1, title='他书第一卷')
        db.add(volume)
        await db.flush()
        foreign = Chapter(volume_id=volume.id, chapter_idx=1, title='不可暴露的他书章名',
                          outline_json={'summary': '他书内容'}, content_text='他书原稿。')
        db.add(foreign)
        for pid in [r.project, other.id]:
            db.add(Outline(project_id=pid, level='book', is_confirmed=True,
                           content_json={'summary': '完整全书故事'}))
            db.add(Outline(project_id=pid, level='volume', is_confirmed=True,
                           content_json={'volume_idx': 1, 'summary': '第一卷完整故事'}))
        await db.commit()
        return SimpleNamespace(project=str(r.project), chapter=str(r.chapter), volume=str(own.volume_id),
                               foreign_project=str(other.id), foreign_chapter=str(foreign.id), foreign_volume=str(volume.id))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['chapter', 'volume', 'conflicting_volume'])
async def test_foreign_target_is_never_ready_or_exposed(runtime_db, books, kind):
    target = {
        'chapter': {'chapter_id': books.foreign_chapter},
        'volume': {'volume_id': books.foreign_volume, 'chapter_idx': 1},
        'conflicting_volume': {'chapter_id': books.chapter, 'volume_id': books.foreign_volume},
    }[kind]
    async with runtime_db.sessions() as db:
        valid = await build_outline_readiness_report(db, project_id=books.project, chapter_id=books.chapter)
        assert valid.ready
        report = await build_outline_readiness_report(db, project_id=books.project, **target)
        assert not report.ready
        assert report.target_error == 'target_scope_mismatch'
        assert report.layers['book'].ready
        assert not report.layers['volume'].ready and not report.layers['chapter'].ready
        assert report.layers['chapter'].title is None and report.volume_idx is None
        assert '不匹配' in report.block_message()
        assert '不可暴露' not in str(report.to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize('task_type', ['chapter', 'outline_chapter'])
async def test_async_api_rejects_before_task_creation_or_queue(runtime_db, books, monkeypatch, task_type):
    from app.api.generate import router
    queue = Mock()
    monkeypatch.setattr('app.tasks.knowledge_tasks.run_async_generation.delay', queue)
    async def database():
        async with runtime_db.sessions() as db:
            yield db
            await db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/generate/async', json={
            'project_id': books.project, 'chapter_id': books.foreign_chapter, 'task_type': task_type})
        assert response.status_code == 422
        assert response.json()['detail']['readiness']['target_error'] == 'target_scope_mismatch'
        assert '不匹配' in response.json()['detail']['message']
    queue.assert_not_called()
    async with runtime_db.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(GenerationTask)) == 0
        assert (await db.get(Chapter, books.foreign_chapter)).content_text == '他书原稿。'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['scene', 'single', 'stream'])
async def test_direct_generators_reject_before_context_or_model(runtime_db, books, monkeypatch, mode):
    from app.services import scene_orchestrator, chapter_generator
    module = scene_orchestrator if mode == 'scene' else chapter_generator
    builder = Mock()
    monkeypatch.setattr(module, 'ContextPackBuilder', builder)
    generator = scene_orchestrator.SceneOrchestrator() if mode == 'scene' else chapter_generator.ChapterGenerator()
    async with runtime_db.sessions() as db:
        kwargs = dict(project_id=books.project, volume_id=books.foreign_volume,
                      chapter_id=books.foreign_chapter, chapter_idx=1, db=db)
        with pytest.raises(RuntimeError, match='不匹配'):
            if mode == 'single':
                await generator.generate(**kwargs)
            else:
                method = generator.orchestrate_chapter_stream if mode == 'scene' else generator.generate_stream
                async for _ in method(**kwargs):
                    pass
    builder.assert_not_called()
