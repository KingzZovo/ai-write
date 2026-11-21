"""Prompt completeness: executable examples and no silent contract loss."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import prompt_registry
from app.services.chapter_outline_expander import ChapterOutlineExpandError, _format_user_prompt, _validate_and_normalize
from app.services.context_pack import ContextPack


def chapter():
    return SimpleNamespace(chapter_idx=4, title='已确认标题')


def test_example_schema_is_valid_json():
    ctx = dict(book_outline={}, volume_outline={}, prev_outline={}, prev_text='', stub={})
    payload = json.loads(_format_user_prompt(ctx, chapter()).split('输出 schema (JSON 严格遵守)：', 1)[1].split('额外要求：', 1)[0].strip())
    assert isinstance(payload['state_changes']['characters'][0], dict)
    assert isinstance(payload['foreshadows_planted'][0], dict)


def test_normalization_retains_action_and_inference_contracts():
    raw = {'summary': '行动过程。', 'key_events': ['发现证据'], 'action_budget': '十秒内只能开一扇门', 'inference_ledger': '只有足迹，不能确定身份'}
    result = _validate_and_normalize(raw, chapter(), {})
    assert result['action_budget'] == raw['action_budget']
    assert result['inference_ledger'] == raw['inference_ledger']


def test_structured_contracts_reach_writer_context():
    raw = {'summary': '交接情报。', 'key_events': ['拿到信件'], 'entity_transfers': [{'item': '密信', 'from': '信使', 'to': '守卫'}],
           'information_state': {'守卫': {'known': ['信使失踪'], 'unknown': ['幕后身份']}}, 'time_delta': 0}
    result = _validate_and_normalize(raw, chapter(), {})
    prompt = ContextPack(current_outline=result).to_system_prompt(token_budget=16000)
    for fact in ('密信', '信使失踪', '幕后身份', '时间消耗：0'):
        assert fact in prompt


def test_renderer_also_supports_existing_structured_outlines():
    assert '北岸' in ContextPack()._render_chapter_outline_block({'title': '桥头', 'start_state': {'location': '北岸'}})


def test_expander_cannot_renumber_or_erase_existing_obligations():
    stub = {'foreshadows_planted': [{'description': '旧承诺'}], 'prev_chapter_threads': ['追问失踪者'],
            'state_changes': {'characters': [{'name': '阿青', 'change': '负伤'}]}}
    result = _validate_and_normalize({'chapter_idx': 99, 'title': '擅改', 'summary': '新过程。', 'key_events': ['新动作']}, chapter(), stub)
    assert result['chapter_idx'] == 4 and result['title'] == '已确认标题'
    assert result['foreshadows_planted'] == stub['foreshadows_planted']
    assert result['state_changes']['characters'] == stub['state_changes']['characters']


@pytest.mark.parametrize('payload', [{'_parse_error': True}, {'parse_error': True}, {}])
def test_unusable_expansion_is_rejected(payload):
    with pytest.raises(ChapterOutlineExpandError):
        _validate_and_normalize(payload, chapter(), {'summary': '原细纲。'})


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [None, 16384])
async def test_json_retry_preserves_original_output_budget(monkeypatch, budget):
    generate = AsyncMock(side_effect=[SimpleNamespace(text=''), SimpleNamespace(text='{"summary":"完整细纲"}')])
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', generate)
    options = {'max_tokens': budget} if budget else {}
    await prompt_registry.run_structured_prompt('outline_chapter', '输出全部细纲', None, **options)
    retry = generate.await_args_list[1]
    assert retry.kwargs.get('max_tokens') == budget
    assert '几百' not in retry.args[3]
    assert '完整' in retry.args[3]


@pytest.mark.asyncio
async def test_structurally_valid_but_incomplete_payload_gets_one_repair(monkeypatch):
    from app.services.chapter_outline_expander import _validate_expansion_payload
    complete = {'summary': '完整过程。', 'key_events': ['进入', '交接']}
    generate = AsyncMock(side_effect=[SimpleNamespace(text='{"summary":"只有梗概"}'), SimpleNamespace(text=json.dumps(complete))])
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', generate)
    result = await prompt_registry.run_structured_prompt('outline_chapter', '任务', None,
                                                        output_validator=_validate_expansion_payload, max_tokens=12000)
    assert result == complete and generate.await_count == 2
    assert 'key_events' in generate.await_args_list[1].args[3]
    assert all('output_validator' not in call.kwargs for call in generate.await_args_list)


@pytest.mark.asyncio
async def test_invalid_repair_remains_explicit_failure(monkeypatch):
    from app.services.chapter_outline_expander import _validate_expansion_payload
    generate = AsyncMock(return_value=SimpleNamespace(text='{}'))
    monkeypatch.setattr(prompt_registry, 'run_text_prompt', generate)
    result = await prompt_registry.run_structured_prompt('outline_chapter', '任务', None, output_validator=_validate_expansion_payload)
    assert result['parse_error'] is True and generate.await_count == 2
    with pytest.raises(ChapterOutlineExpandError):
        _validate_and_normalize(result, chapter(), {})


def test_scene_sequence_and_legacy_text_both_survive_rendering():
    raw = {'summary': '完整过程。', 'key_events': ['交接'], 'start_state': '从北岸出发',
           'scene_beats': [{'goal': '过桥', 'obstacle': '守卫封锁'}, {'turn': '出示通行证', 'cost': '暴露身份'}]}
    result = _validate_and_normalize(raw, chapter(), {})
    prompt = ContextPack(current_outline=result).to_system_prompt(token_budget=16000)
    assert all(text in prompt for text in ('从北岸出发', '守卫封锁', '暴露身份'))


@pytest.mark.parametrize('changes', [
    {'characters': ['阿青负伤']},
    {'items': [{'name': '密信'}]},
    {'relationships': [{'from': '阿青', 'change': '结盟'}]},
])
def test_state_changes_cannot_silently_disappear_in_renderer(changes):
    with pytest.raises(ChapterOutlineExpandError, match='条目缺少有效字段'):
        _validate_and_normalize({'summary': '完整过程。', 'key_events': ['交接'],
                                 'state_changes': changes}, chapter(), {})


@pytest.mark.parametrize('event', [0, True, [], {}])
def test_non_event_values_are_not_accepted_as_plot(event):
    with pytest.raises(ChapterOutlineExpandError, match='关键事件'):
        _validate_and_normalize({'summary': '完整过程。', 'key_events': [event]}, chapter(), {})


def test_non_schema_outline_fields_are_not_silently_dropped():
    """标题之外的字段名不是内置 schema 时,也不能只交出一个标题。

    旧实现只要渲染出标题行,就认为"有可识别内容",于是 brief/pov/location
    这类字段被整块丢掉:整章大纲等于没给,第 2 章起模型只能顺着上一章往下写。
    """
    block = ContextPack()._render_chapter_outline_block({
        'chapter_idx': 2, 'title': '第二章 顶针与八百块',
        'brief': '房东来催下半年铺租八百元。', 'pov': '周芸',
        'location': '周芸的缝纫铺', 'time_cue': '次日清晨'})
    for fact in ('房东来催下半年铺租八百元', '周芸', '缝纫铺', '次日清晨'):
        assert fact in block


def test_schema_outline_is_rendered_once_without_duplication():
    block = ContextPack()._render_chapter_outline_block({
        'chapter_idx': 3, 'title': '第三章', 'summary': '周航被老陈堵在铺子里。',
        'key_events': ['老陈要结上月货款七百']})
    assert block.count('周航被老陈堵在铺子里') == 1
    assert 'summary:' not in block and 'key_events:' not in block
