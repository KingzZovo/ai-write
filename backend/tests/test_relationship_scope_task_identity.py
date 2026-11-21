"""Task identity reaches durable retry ownership without contacting a broker."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.tasks import entity_tasks
from app.services import entity_projection


def test_celery_task_forwards_original_request_id_and_keeps_finite_retries(monkeypatch):
    runner = AsyncMock(return_value={'status': 'ok'})
    monkeypatch.setattr(entity_tasks, '_extract_chapter_async', runner)
    monkeypatch.setattr('app.tasks._run_async_safe', asyncio.run)
    task = entity_tasks.extract_chapter_entities
    task.push_request(id='same-celery-id-across-retries', retries=2)
    try:
        assert task.run(project_id='project', chapter_idx=4, caller='entity_projection_replay', chapter_id='chapter') == {'status': 'ok'}
    finally:
        task.pop_request()
    assert task.max_retries == 3
    runner.assert_awaited_once_with(project_id='project', chapter_idx=4, caller='entity_projection_replay',
                                    chapter_id='chapter', delivery_id='same-celery-id-across-retries')


@pytest.mark.asyncio
async def test_async_task_bridge_passes_delivery_identity_to_projection(monkeypatch):
    source = NS(project_id='project', chapter_id='chapter')
    driver = MagicMock()
    projection = AsyncMock(return_value={'status': 'ok'})
    close = AsyncMock()
    monkeypatch.setattr(entity_tasks, '_resolve_chapter', AsyncMock(return_value=source))
    monkeypatch.setattr(entity_projection, 'ensure_record', AsyncMock(return_value='record'))
    monkeypatch.setattr(entity_projection, 'run_entity_projection', projection)
    monkeypatch.setattr('app.db.neo4j.init_neo4j', AsyncMock())
    monkeypatch.setattr('app.db.neo4j.close_neo4j', close)
    monkeypatch.setattr('app.db.neo4j._driver', driver)
    monkeypatch.setattr('app.services.entity_timeline.EntityTimelineService.initialize_graph', AsyncMock())
    assert (await entity_tasks._extract_chapter_async('project', 4, 'entity_projection_replay',
                'chapter', delivery_id='original-job'))['status'] == 'ok'
    assert projection.await_args.kwargs['source'] is source
    assert projection.await_args.kwargs['delivery_id'] == 'original-job'
    close.assert_awaited_once()
