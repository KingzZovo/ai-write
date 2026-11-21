"""Validate the entire extraction before the first graph mutation."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from app.services.chapter_entity_extraction import (
    FIELDS, EntityExtractionError, extract_entity_changes, parse_entity_changes,
)
from app.services.entity_timeline import EntityTimelineService
from tests.test_pipeline_control import runtime_db

SOURCE = '江枫走进酒馆。他把四百文放在柜台上。'

def payload():
    data = {key: [] for key in FIELDS}
    data['new_characters'] = [{'name': '江枫', 'state': {'状态': '在酒馆'}, 'evidence': '江枫走进酒馆。'}]
    return data

@pytest.mark.parametrize('case', ['missing', 'wrong_schema', 'wrong_array', 'bad_record', 'missing_name',
                                 'empty_name', 'bad_state', 'bad_evidence', 'duplicate', 'nonfinite', 'unclosed', 'nested_nul', 'surrogate'])
def test_invalid_payload_is_rejected_as_a_whole(case):
    data = payload()
    if case == 'missing': data.pop('state_changes')
    if case == 'wrong_schema': data = {'characters': [], 'relationships': [], 'locations': []}
    if case == 'wrong_array': data['state_changes'] = {}
    if case == 'bad_record': data['location_changes'] = ['地点']
    if case == 'missing_name': data['new_characters'][0].pop('name')
    if case == 'empty_name': data['new_characters'][0]['name'] = ' '
    if case == 'bad_state': data['new_characters'][0]['state'] = '受伤'
    if case == 'bad_evidence': data['new_characters'][0]['evidence'] = '江枫飞上屋顶。'
    if case == 'nonfinite': data['new_characters'][0]['state'] = {'钱': float('nan')}
    if case == 'nested_nul': data['new_characters'][0]['state'] = {'细节': ['\x00']}
    if case == 'surrogate': data['new_characters'][0]['state'] = {'细节': '\ud800'}
    raw = json.dumps(data, ensure_ascii=False)
    if case == 'duplicate': raw = raw.replace('"state_changes": []', '"state_changes": [], "state_changes": []')
    if case == 'unclosed': raw = '```json\n' + raw
    with pytest.raises(EntityExtractionError): parse_entity_changes(raw, SOURCE)

@pytest.mark.asyncio
async def test_complete_source_and_one_repair_keep_original_valid_changes(monkeypatch):
    source = SOURCE + '中间正文。' * 1600 + '章末事实。'
    generate = AsyncMock(side_effect=[SimpleNamespace(text='{"characters": []}'), SimpleNamespace(text=json.dumps(payload(), ensure_ascii=False))])
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    assert await extract_entity_changes(project_id='p', chapter_text=source, db=object()) == payload()
    assert generate.await_count == 2
    for call in generate.call_args_list:
        assert source in call.kwargs['user_content']
        assert call.kwargs['task_type'] == 'chapter_entities'
    assert '{"characters": []}' in generate.call_args.kwargs['user_content']

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['invalid', 'provider', 'cancel'])
async def test_failed_extraction_never_starts_graph_writes(monkeypatch, failure):
    import asyncio
    generate = AsyncMock(return_value=SimpleNamespace(text='{}'))
    if failure == 'provider': generate.side_effect = RuntimeError('offline')
    if failure == 'cancel': generate.side_effect = asyncio.CancelledError()
    monkeypatch.setattr('app.services.prompt_registry.run_text_prompt', generate)
    driver = MagicMock()
    error = {'invalid': EntityExtractionError, 'provider': RuntimeError, 'cancel': asyncio.CancelledError}[failure]
    with pytest.raises(error):
        await EntityTimelineService(driver, strict_writes=True).extract_and_update('p', 2, SOURCE)
    driver.session.assert_not_called()
    assert generate.await_count == (2 if failure == 'invalid' else 1)

@pytest.mark.asyncio
async def test_late_invalid_entry_cannot_partially_write_earlier_character():
    data = payload()
    data['location_changes'] = [{'character': '江枫', 'location': '屋顶', 'evidence': '正文没有这句话'}]
    driver = MagicMock()
    with pytest.raises(EntityExtractionError):
        await EntityTimelineService(driver, strict_writes=True).apply_entity_changes('p', 2, SOURCE, data)
    driver.session.assert_not_called()

@pytest.mark.asyncio
async def test_chapter_state_is_not_inserted_as_prestory_state():
    service = EntityTimelineService(MagicMock(), strict_writes=True)
    service.add_character = AsyncMock()
    service.update_character_state = AsyncMock()
    await service.apply_entity_changes('p', 8, SOURCE, payload())
    service.add_character.assert_awaited_once_with('p', '江枫')
    service.update_character_state.assert_awaited_once_with('p', '江枫', 8, {'状态': '在酒馆'})

@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', [
    ('initialize_graph', ('p',)), ('add_character', ('p', '江枫')),
    ('update_character_state', ('p', '江枫', 2, {'状态': '受伤'})),
    ('get_character_state_at', ('p', '江枫', 2)),
    ('add_relationship', ('p', '江枫', '店主', '相识', 2)),
    ('update_relationship', ('p', '江枫', '店主', '敌对', 2)),
    ('_set_character_location', ('p', '江枫', '酒馆', 2)),
])
async def test_graph_failures_propagate_for_extraction(method, args):
    driver = MagicMock()
    driver.session.side_effect = RuntimeError('graph unavailable')
    with pytest.raises(RuntimeError, match='graph unavailable'):
        await getattr(EntityTimelineService(driver, strict_writes=True), method)(*args)

@pytest.mark.asyncio
async def test_seed_inherits_route_but_never_generic_entity_schema(runtime_db):
    from tests.test_prompt_cache_transactions import tables
    from app.models.project import LLMEndpoint
    from app.models.prompt import PromptAsset
    from app.services.prompt_registry import PromptRegistry
    r = runtime_db
    await tables(r)
    async with r.sessions() as db:
        endpoint = LLMEndpoint(name='entities', provider_type='openai', default_model='test')
        db.add(endpoint); await db.flush()
        parent = PromptAsset(task_type='extraction', name='reference', system_prompt='参考书schema',
                             endpoint_id=endpoint.id, model_name='extract-model')
        db.add(parent); await db.commit()
        await PromptRegistry(db).seed_builtins(); await db.commit()
        child = await db.scalar(select(PromptAsset).where(PromptAsset.task_type == 'chapter_entities'))
        assert child.endpoint_id == parent.endpoint_id and child.model_name == 'extract-model'
        assert set(child.output_schema) == set(FIELDS)
        assert child.system_prompt != parent.system_prompt
        child.system_prompt = '作者配置'
        await db.commit()
        assert await PromptRegistry(db).seed_builtins() == 0
        assert child.system_prompt == '作者配置'

@pytest.mark.asyncio
async def test_state_change_ensures_character_and_preserves_prior_properties():
    service = EntityTimelineService(MagicMock(), strict_writes=True)
    service.add_character = AsyncMock()
    service.get_character_state_at = AsyncMock(return_value=SimpleNamespace(status={'身份': '船工', '状态': '未到'}))
    service.update_character_state = AsyncMock()
    data = {key: [] for key in FIELDS}
    data['state_changes'] = [{'name': '江枫', 'changes': {'状态': '在酒馆'}, 'evidence': '江枫走进酒馆。'}]
    await service.apply_entity_changes('p', 8, SOURCE, data)
    service.add_character.assert_awaited_once_with('p', '江枫')
    service.update_character_state.assert_awaited_once_with('p', '江枫', 8, {'身份': '船工', '状态': '在酒馆'})

@pytest.mark.asyncio
async def test_empty_new_character_state_still_has_chapter_origin():
    service = EntityTimelineService(MagicMock(), strict_writes=True)
    service.add_character = AsyncMock()
    service.update_character_state = AsyncMock()
    data = payload(); data['new_characters'][0]['state'] = {}
    await service.apply_entity_changes('p', 8, SOURCE, data)
    service.update_character_state.assert_awaited_once_with('p', '江枫', 8, {})
