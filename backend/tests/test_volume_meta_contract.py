import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outline_contract import validate_volume_meta, VOLUME_NARRATIVE_FIELDS
from app.services.outline_generator import OutlineGenerator
from tests.support.volume_meta import valid_volume_meta


@pytest.mark.parametrize('field', [*VOLUME_NARRATIVE_FIELDS, 'title', 'chapter_count', 'volume_idx', 'turning_points', 'foreshadows', 'new_characters', 'departing_characters'])
def test_missing_obligation_rejected_without_mutating_input(field):
    meta = valid_volume_meta()
    del meta[field]
    before = copy.deepcopy(meta)
    with pytest.raises(ValueError):
        validate_volume_meta(meta, 1)
    assert meta == before


@pytest.mark.parametrize('patch', [
    {'volume_idx': 2}, {'volume_idx': True}, {'chapter_count': '2'},
    {'turning_points': []}, {'turning_points': ['a'] * 4 + ['']},
    {'volume_start_state': {'place': ''}}, {'new_characters': ['somebody']},
    {'departing_characters': [True]}, {'foreshadows': {'planted': [''], 'resolved': []}},
])
def test_malformed_contract_rejected(patch):
    meta = valid_volume_meta()
    meta.update(patch)
    with pytest.raises(ValueError):
        validate_volume_meta(meta, 1)


@pytest.mark.asyncio
async def test_repaired_meta_preserves_context_before_starting_batches(monkeypatch):
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=(2, 2))
    complete = valid_volume_meta()
    incomplete = copy.deepcopy(complete)
    del incomplete['volume_end_state']
    requests = []
    async def generate(task_type, messages, **kwargs):
        requests.append((task_type, copy.deepcopy(messages)))
        if len(requests) == 1:
            return SimpleNamespace(text=json.dumps(incomplete, ensure_ascii=False))
        if len(requests) == 2:
            assert task_type == 'outline_volume_meta'
            assert 'volume_end_state' in messages[-1]['content']
            assert json.loads(messages[-2]['content']) == incomplete
            assert '必须等于 2' in messages[1]['content']
            return SimpleNamespace(text=json.dumps(complete, ensure_ascii=False))
        assert task_type == 'outline_volume_batch'
        return SimpleNamespace(text=json.dumps({'batch': [
            {'chapter_idx': i, 'title': f'桥头{i}', 'summary': '交接信件后离开', 'key_events': ['交接信件', '离开桥头']}
            for i in (1, 2)
        ]}, ensure_ascii=False))
    obj.router = SimpleNamespace(generate=generate)
    monkeypatch.setattr('app.services.title_quality_checker.check_and_rewrite_in_place', AsyncMock(return_value={}))
    result = await obj._generate_volume_outline_staged({'raw_text': '全书前提'}, 1)
    assert result['volume_end_state'] == complete['volume_end_state']
    assert len(result['chapter_summaries']) == 2
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_router_configuration_error_is_not_a_json_repair_or_template():
    obj = OutlineGenerator()
    obj._load_chapters_per_volume_pref = AsyncMock(return_value=None)
    call = AsyncMock(side_effect=ValueError('no configured endpoint'))
    obj.router = SimpleNamespace(generate=call)
    result = await obj._generate_volume_outline_staged({}, 1)
    assert result['_parse_error']
    assert result['chapter_summaries'] == []
    call.assert_awaited_once()


@pytest.mark.parametrize('count', [1, 3, 5])
def test_short_complete_story_accepts_natural_number_of_turning_points(count):
    meta = valid_volume_meta(3)
    meta.update(turning_points=['林禾说明缺页无法补全，决定先兑现家谱交付。'] * count,
                transition_to_next='全书完结，旧书与工具带离，林禾带走菜谱。',
                foreshadow_progression='本卷无伏笔')
    before = copy.deepcopy(meta)
    validate_volume_meta(meta, 1, (3, 3))
    assert meta == before
