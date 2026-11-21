from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.outline_generator import OutlineGenerator


def section(n, text='人物采取行动并付出代价，局势随之变化。'):
    return f'{n}、段落\n' + text * 90


def skeleton():
    return '\n'.join(section(n) for n in '一三七八九') + '\n<volume-plan>[{"idx":1,"title":"第一卷","est_chapters":20}]</volume-plan>'


def generator(router):
    obj=OutlineGenerator.__new__(OutlineGenerator)
    obj.router=router; obj.project_id=None; obj.chapter_naming_directive=''
    return obj


@pytest.mark.asyncio
async def test_empty_skeleton_does_not_turn_into_template_success():
    call=AsyncMock(return_value=SimpleNamespace(text=''))
    obj=generator(SimpleNamespace(generate=call))
    with pytest.raises(ValueError,match='骨架生成失败'):
        await obj._generate_book_outline_staged('作者创意')
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_missing_character_stage_does_not_turn_into_generic_characters():
    call=AsyncMock(side_effect=[SimpleNamespace(text=skeleton()),SimpleNamespace(text=''),SimpleNamespace(text='')])
    obj=generator(SimpleNamespace(generate=call))
    with pytest.raises(ValueError,match='B2/characters'):
        await obj._generate_book_outline_staged('作者创意')
    assert call.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['skeleton','characters','short_world'])
async def test_stream_failure_never_emits_done(failure):
    async def stream(task_type,messages,**kwargs):
        prompt=messages[0]['content']
        if '骨架' in messages[1]['content'] and '创意：' in messages[1]['content'] and '已生成的骨架' not in messages[1]['content']:
            if failure=='skeleton': raise TimeoutError('断流')
            yield skeleton()
        elif '二、四、五' in messages[1]['content']:
            yield '\n'.join(section(n) for n in '二四五')
            if failure=='characters': raise TimeoutError('人物阶段断流')
        else:
            yield '六、世界观\n太短' if failure=='short_world' else section('六')
    obj=generator(SimpleNamespace(generate_stream=stream,generate=AsyncMock(return_value=SimpleNamespace(text=''))))
    events=[]
    with pytest.raises(ValueError):
        async for event in obj._generate_book_outline_staged_stream('作者创意'):
            events.append(event)
    assert not any(e['event']=='done' for e in events)


@pytest.mark.asyncio
async def test_complete_nonstream_outline_retains_all_owned_sections():
    texts=[skeleton(),section('二'),section('四'),section('五'),section('六')]
    obj=generator(SimpleNamespace(generate=AsyncMock(side_effect=[SimpleNamespace(text=t) for t in texts])))
    result=await obj._generate_book_outline_staged('作者创意')
    assert [c.kwargs['task_type'] for c in obj.router.generate.await_args_list] == [
        'outline_book_skeleton', 'outline_book_characters', 'outline_book_characters',
        'outline_book_characters', 'outline_book_world']
    assert all(result['_stages'].values()) and result['volume_plan']
    assert all(n+'、' in result['raw_text'] for n in '一二三四五六七八九')


def test_section_ownership_prevents_wrong_stage_overriding_character_plan():
    obj=generator(None)
    result=obj._reassemble_sections(skeleton()+'\n'+section('二','骨架越权人物'),section('二','人物阶段正稿'),section('六'))
    assert '人物阶段正稿' in result and '骨架越权人物' not in result


def test_long_output_missing_required_sections_is_not_complete():
    with pytest.raises(ValueError,match='缺少正文段落'):
        generator(None)._validate_book_stage(section('二'), 'B', 1200)


@pytest.mark.asyncio
async def test_complete_stream_emits_one_valid_done():
    async def stream(task_type,messages,**kwargs):
        request=messages[1]['content']
        if '已生成的骨架' not in request:
            yield skeleton()
        elif '二、四、五' in request:
            yield '\n'.join(section(n) for n in '二四五')
        else:
            yield section('六')
    obj=generator(SimpleNamespace(generate_stream=stream))
    events=[e async for e in obj._generate_book_outline_staged_stream('作者创意')]
    done=[e for e in events if e['event']=='done']
    assert len(done)==1 and all(done[0]['_stages'].values())
    assert done[0]['volume_plan']


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_short_book_prompts_and_complete_compact_stages(streaming):
    from app.services.outline_generator import compute_scale
    scale = compute_scale(5400, chapter_words=1800, chapters_per_volume_min=3,
                          chapters_per_volume_max=3, chapters_per_volume_target=3)
    captured = []
    def compact(n):
        return f'{n}、段落\n' + '林禾决定先修家谱，周砚说明旧书缺文不能凭空补造。' * 14
    a = '\n'.join(compact(n) for n in '一三七八九') + '\n<volume-plan>[{"idx":1,"title":"修补以后","est_chapters":3}]</volume-plan>'
    async def produce(task_type, messages, **kwargs):
        captured.append(messages)
        if task_type == 'outline_book_skeleton':
            return a
        if task_type == 'outline_book_world':
            return compact('六')
        request = messages[-1]['content']
        if '请只生成二' in request: return compact('二')
        if '请只生成四' in request: return compact('四')
        if '请只生成五' in request: return compact('五')
        return '\n'.join(compact(n) for n in '二四五')
    async def generate(**kwargs):
        return SimpleNamespace(text=await produce(**kwargs))
    async def stream(**kwargs):
        yield await produce(**kwargs)
    obj = generator(SimpleNamespace(generate=generate, generate_stream=stream))
    if streaming:
        result = [e async for e in obj._generate_book_outline_staged_stream('现实题材修书短篇', scale=scale)][-1]
        assert result['event'] == 'done'
    else:
        result = await obj._generate_book_outline_staged('现实题材修书短篇', scale=scale)
    assert all(result['_stages'].values())
    assert result['volume_plan'][0]['est_chapters'] == 3
    assert '必须输出 1 卷' in captured[0][0]['content']
    assert '总章数目标 3 章' in captured[0][0]['content']
    all_messages = '\n'.join(m['content'] for call in captured for m in call)
    assert sum('【世界设定段的规则汇总】' in call[0]['content'] for call in captured) == 1
    assert all('不应以“未指定”为由跳过关键物品去向' in call[0]['content'] for call in captured)
    for obsolete in ['至少写 5', '至少 5 人', '至少5人', '至少 5 行', '关键创伤：',
                     '修为境界/关键能力', '道具武器功法', '不少于 1800', '100–200 章/卷']:
        assert obsolete not in all_messages


@pytest.mark.parametrize('prefix', ['', '## ', '  ### '])
def test_numbered_markdown_headings_preserve_stage_content(prefix):
    obj = generator(None)
    text = '\n'.join(prefix + section(n) for n in '一三七八九')
    text += '\n<volume-plan>[{"idx":1,"title":"修补以后","est_chapters":3}]</volume-plan>'
    obj._validate_book_stage(text, 'A', 1200)
    parts = dict(obj._iter_sections(text))
    assert list(parts) == list('一三七八九')
    assert all(value.startswith(n + '、') for n, value in parts.items())
    assert all(section(n) in parts[n] for n in parts)
    assert '<volume-plan>' in parts['九']
