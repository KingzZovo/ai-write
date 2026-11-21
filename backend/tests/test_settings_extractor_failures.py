import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.services import settings_extractor as service
from tests.test_outline_settings_source import source_db

EMPTY = {'characters': [], 'world_rules': [], 'relationships': []}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['valid_empty', 'router_failed', 'call_failed', 'bad_json', 'missing_list', 'invalid_item', 'unknown_person', 'duplicate_person'])
async def test_combined_extract_distinguishes_empty_results_from_failures(monkeypatch, mode):
    monkeypatch.setattr(service, '_settings_split_enabled', lambda: False)
    data = dict(EMPTY)
    if mode == 'missing_list': data.pop('characters')
    if mode == 'invalid_item': data['characters'] = [{'name': True}]
    if mode == 'unknown_person': data['relationships'] = [{'source_name': '甲', 'target_name': '乙', 'rel_type': 'family'}]
    if mode == 'duplicate_person': data['characters'] = [{'name': '甲'}, {'name': '甲'}]
    router = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text='bad' if mode == 'bad_json' else json.dumps(data)),
        side_effect=RuntimeError('failed') if mode == 'call_failed' else None))
    loader = AsyncMock(return_value=router, side_effect=RuntimeError('router unavailable') if mode == 'router_failed' else None)
    monkeypatch.setattr(service, 'get_model_router_async', loader)
    project_id = uuid4()
    if mode == 'valid_empty': assert await service.extract_settings_from_outline('作者原文', project_id=project_id) == EMPTY
    else:
        with pytest.raises(service.SettingsExtractionError): await service.extract_settings_from_outline('作者原文', project_id=project_id)
    loader.assert_awaited_once()
    if mode == 'router_failed': router.generate.assert_not_awaited()
    else: assert router.generate.call_args.kwargs['_log_meta']['project_id'] == str(project_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['all_success', 'one_failure', 'all_failure', 'cancelled'])
async def test_split_result_is_complete_or_fails_without_silent_empty_branches(monkeypatch, mode):
    monkeypatch.setattr(service, '_settings_split_enabled', lambda: True)
    async def generate(**kwargs):
        task = kwargs['task_type']
        if task == 'extraction': return SimpleNamespace(text=json.dumps(EMPTY))
        if mode == 'all_failure' or (mode == 'one_failure' and task == 'characters_extraction'): raise RuntimeError('failed')
        if mode == 'cancelled' and task == 'characters_extraction': raise asyncio.CancelledError
        field = {'characters_extraction': 'characters', 'world_rules_extraction': 'world_rules', 'relationships_extraction': 'relationships'}[task]
        return SimpleNamespace(text=json.dumps({field: []}))
    router = SimpleNamespace(generate=AsyncMock(side_effect=generate))
    monkeypatch.setattr(service, 'get_model_router_async', AsyncMock(return_value=router))
    if mode == 'one_failure':
        with pytest.raises(service.SettingsExtractionError, match='未写入'): await service.extract_settings_from_outline('原文')
    elif mode == 'cancelled':
        with pytest.raises(asyncio.CancelledError): await service.extract_settings_from_outline('原文')
    else:
        assert await service.extract_settings_from_outline('原文') == EMPTY
    assert router.generate.await_count == (4 if mode == 'all_failure' else 3)


@pytest.mark.asyncio
async def test_endpoint_does_not_initialize_graph_after_model_failure(monkeypatch):
    from app.api import outlines as api
    project, outline, db = source_db()
    monkeypatch.setattr(api, 'extract_settings_from_outline', AsyncMock(side_effect=service.SettingsExtractionError('提取失败')))
    graph = AsyncMock()
    monkeypatch.setattr(api, 'init_neo4j', graph)
    with pytest.raises(HTTPException) as exc:
        await api.extract_settings(str(project.id), str(outline.id), db)
    assert exc.value.status_code == 502
    graph.assert_not_awaited()
    assert db.scalar.await_count == 3  # Source plus receipt lookup; no write-phase locks.
